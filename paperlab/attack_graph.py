#!/usr/bin/env python3
# -*- coding: utf-8 -*-
#
# Project: PaperLab - AI Automated OSCP Lab Generator
# Author: tw1t
# Copyright 2026 tw1t
# SPDX-License-Identifier: Apache-2.0
"""
攻击链结构化建模与逻辑校验。

## 为什么需要这一层

原来靶机质量全靠一句 prompt 保证："要合理、要有逻辑"。但**格式符合要求，
不等于攻击链成立** —— LLM 完全可能写出"日志里从没出现过 SMB，题目却要求用
SMB 凭证横向"这种一眼假的靶机，而旧的 quality_check() 只看语言、难度、
题目与考点是否对应，根本查不出逻辑断裂。

这一层把"攻击链"显式化成一组节点，然后做机器可校验的检查。它不追求
"理解渗透测试"（那需要另一个模型，成本高且不稳定），只抓住三条**最容易量化、
也最容易出错**的规则：

1. 证据必须能在终端日志里找到（防幻觉：情报里没出现的东西不能当证据）
2. 阶段不许跳、不许倒退（防因果断裂）
3. 侦察 / 初始访问 / 提权 三个阶段必须齐全（OSCP 纸上推演的基本结构）

## 节点的形状

    {
      "stage": "initial_access",        # 见 CHAIN_STAGES
      "hypothesis": "SMB 上的备份目录可能含可复用凭证",   # 要验证的假设
      "evidence": "AdminBackup: Fall2024!@#",            # ⚠️ 必须是日志里的原样片段
      "action": "用该凭证尝试 SMB / SSH 登录",             # 对应动作
      "expected_result": "获得低权限 shell"               # 预期结果
    }

`evidence` 要求"原样片段"是刻意的：它既是给学生的线索索引，也是我们唯一能
自动核对的东西。写不出原样片段，通常意味着这条链是编的。
"""

import re

# 阶段顺序即因果顺序：后面的阶段依赖前面阶段获得的信息。
CHAIN_STAGES = [
    "recon",              # 信息收集：端口、服务、版本
    "vuln_discovery",     # 漏洞发现：从指纹定位可验证的入口
    "initial_access",     # 初始访问：拿到低权限立足点
    "cred_discovery",     # 凭证发现：配置文件/数据库里的凭据
    "lateral",            # 横向/用户切换：凭证复用
    "privilege_escalation",  # 提权：拿到 root/SYSTEM
]

STAGE_LABELS = {
    "recon": "信息收集",
    "vuln_discovery": "漏洞发现",
    "initial_access": "初始访问",
    "cred_discovery": "凭证发现",
    "lateral": "横向利用",
    "privilege_escalation": "权限提升",
}

# 一台合格的纸上靶机至少要能推演出这三步
REQUIRED_STAGES = ("recon", "initial_access", "privilege_escalation")

REQUIRED_FIELDS = ("stage", "hypothesis", "evidence", "action", "expected_result")

# 判定 evidence 与 context 是否对得上时忽略的词：
# 这些词在两边都必然出现，算进来的话任何证据都会"匹配成功"。
_STOPWORDS = {
    "the", "and", "for", "with", "from", "that", "this", "are", "was", "were",
    "has", "have", "had", "not", "but", "you", "can", "will", "may", "should",
    "into", "then", "than", "there", "here", "their", "them", "our", "its",
    "get", "got", "use", "used", "using", "via", "per", "all", "any", "one",
    "two", "new", "old", "see", "seen", "find", "found", "file", "files",
    "user", "users", "pass", "password", "https", "http", "www",
}

_TOKEN_RE = re.compile(r"[A-Za-z0-9_./\\:@!$%^&*+=~-]{2,}")


def _tokens(text):
    """抽出可比较的词/片段，统一小写。"""
    return {t.lower() for t in _TOKEN_RE.findall(text or "")}


def _distinctive_tokens(text):
    return {t for t in _tokens(text) if t not in _STOPWORDS}


def evidence_supported(evidence, context, min_ratio=0.5):
    """
    evidence 里的特征片段有多少能在终端日志里找到。

    返回 (ok, ratio, missing)。ratio 是"命中比例"，missing 是没找到的那些词，
    用来说明到底哪部分像编的。
    """
    toks = _distinctive_tokens(evidence)
    if not toks:
        # 证据全是停用词/太短 —— 这种情况没法校验，按"不通过"处理更安全
        return False, 0.0, []
    ctx = (context or "").lower()
    missing = sorted(t for t in toks if t not in ctx)
    hit = len(toks) - len(missing)
    ratio = hit / len(toks)
    return ratio >= min_ratio, round(ratio, 2), missing


def validate_chain(chain, context, questions=None):
    """
    校验一条攻击链，返回 issues 列表。每条 issue 是
    {"level": "error"|"warn", "code": ..., "message": ..., "stage": ...}。

    只返回问题；空列表表示通过。
    """
    issues = []

    def err(code, message, stage=None):
        issues.append({"level": "error", "code": code, "message": message, "stage": stage})

    def warn(code, message, stage=None):
        issues.append({"level": "warn", "code": code, "message": message, "stage": stage})

    if not isinstance(chain, list) or not chain:
        err("chain_missing", "缺少 attack_chain（结构化攻击链），无法做逻辑校验")
        return issues

    order = {s: i for i, s in enumerate(CHAIN_STAGES)}
    seen_stages = []
    prev_idx = -1

    for i, node in enumerate(chain):
        if not isinstance(node, dict):
            err("node_not_object", f"第 {i + 1} 个节点不是对象")
            continue

        stage = str(node.get("stage") or "").strip()
        if stage not in order:
            err("stage_unknown",
                f"第 {i + 1} 个节点的 stage 不在允许的枚举里：{stage!r}",
                stage)
            continue

        for f in REQUIRED_FIELDS:
            if not str(node.get(f) or "").strip():
                err("field_missing",
                    f"第 {i + 1} 个节点（{STAGE_LABELS.get(stage, stage)}）缺少字段 {f}",
                    stage)

        # 阶段不许倒退
        idx = order[stage]
        if idx < prev_idx:
            err("stage_backwards",
                f"第 {i + 1} 个节点（{STAGE_LABELS[stage]}）出现在"
                f"（{STAGE_LABELS[CHAIN_STAGES[prev_idx]]}）之后 —— 阶段倒退，因果断裂",
                stage)
        prev_idx = max(prev_idx, idx)
        seen_stages.append(stage)

        # 证据必须在终端日志里找得到
        evidence = str(node.get("evidence") or "")
        if evidence.strip():
            ok, ratio, missing = evidence_supported(evidence, context)
            if not ok:
                detail = "、".join(missing[:5]) if missing else "（无法比对）"
                err("evidence_not_found",
                    f"第 {i + 1} 个节点（{STAGE_LABELS[stage]}）的证据在终端日志里找不到"
                    f"（命中 {ratio:.0%}，缺：{detail}）—— 情报里没出现的东西不能当证据",
                    stage)

    # 三个阶段必须齐全
    for need in REQUIRED_STAGES:
        if need not in seen_stages:
            err("stage_missing",
                f"攻击链缺少必需阶段「{STAGE_LABELS[need]}」—— "
                f"纸面推演至少要覆盖 侦察 → 初始访问 → 提权",
                need)

    # 题目必须能挂到某个阶段上（可选检查：题目里没有 stage 字段时只提示）
    if questions:
        for i, q in enumerate(questions):
            if not isinstance(q, dict):
                continue
            st = str(q.get("chain_stage") or "").strip()
            if st and st not in order:
                err("question_stage_unknown",
                    f"第 {i + 1} 题的 chain_stage 不在枚举里：{st!r}", st)

    return issues


def summarize(issues):
    """把 issues 压成一行人话，用于日志/报错。"""
    if not issues:
        return "攻击链校验通过"
    errors = [i for i in issues if i["level"] == "error"]
    head = errors[:2] or issues[:2]
    text = "；".join(i["message"] for i in head)
    if len(issues) > len(head):
        text += f"（另有 {len(issues) - len(head)} 条）"
    return text


def has_errors(issues):
    return any(i["level"] == "error" for i in issues)


def chain_digest(chain):
    """
    给日志和前端用的紧凑摘要：只留阶段序列和节点数，
    不泄露节点内容（攻击链就是答案，不能出现在任何学生可见的地方）。
    """
    if not isinstance(chain, list):
        return ""
    stages = [str(n.get("stage", "?")) for n in chain if isinstance(n, dict)]
    labels = [STAGE_LABELS.get(s, s) for s in stages]
    return f"{len(stages)} 节点：" + " → ".join(labels)
