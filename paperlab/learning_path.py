#!/usr/bin/env python3
# -*- coding: utf-8 -*-
#
# Project: PaperLab - AI Automated OSCP Lab Generator
# Author: tw1t
# Copyright 2026 tw1t
# SPDX-License-Identifier: Apache-2.0
"""
个性化学习路径。

## 它解决什么

原来的统计只告诉你"各 Domain 平均分是多少"，但那不构成行动建议 ——
学生看到"Linux Privilege Escalation 5.2 分"之后，下一步该干什么仍然要自己想。

这个模块把统计翻译成**阶段化的训练路线**：把题库的 Domain 归到四类技能上，
用答题记录算掌握度，标出"已掌握 / 需加强 / 待学习"，并把待复习的 SM-2 卡片数
一起带出来。

## 设计原则

- **纯函数**：输入是统计数据，输出是路线。不连数据库、不调模型，
  所以可以被单测完全覆盖，也永远不会因为模型抽风而变化。
- **可解释**：每个阶段的掌握度就是该阶段各 Domain 的平均分（按作答次数加权），
  状态阈值写死在下面，学生能自己算出来。
- **不编数据**：没有作答记录的阶段就是"待学习"，不会因为"题库里有一台这类的靶机"
  就假装你练过。
"""

# 四类技能路线。domain 取自 labs.domain 的固定枚举（见 CLAUDE.md 的 Key Constraints）。
STAGES = [
    {
        "key": "recon",
        "name": "阶段一：基础信息收集",
        "desc": "端口/服务识别、目录爆破、共享与匿名访问",
        "domains": ["Network Services"],
    },
    {
        "key": "web",
        "name": "阶段二：Web 漏洞利用",
        "desc": "注入、文件包含、上传、以及它们组合出的初始访问",
        "domains": ["Web Application"],
    },
    {
        "key": "linux",
        "name": "阶段三：Linux 提权",
        "desc": "sudo 配置、SUID、可写 cron、内核与服务缺陷",
        "domains": ["Linux Privilege Escalation"],
    },
    {
        "key": "windows_ad",
        "name": "阶段四：Windows 与 AD",
        "desc": "Windows 提权面、域内凭证与横向",
        "domains": ["Windows Privilege Escalation", "Active Directory", "Internal Network", "Mixed"],
    },
]

# 状态阈值（写死在这里，学生可以自己算）
MASTERED_AT = 0.75   # 掌握度 ≥ 75% 视为已掌握
NEEDS_WORK_AT = 0.50  # ≥ 50% 需加强；低于则为薄弱


def _weighted_mastery(pairs):
    """pairs: [(avg_score_0_10, count)] → 加权掌握度（0-1），没有记录返回 None。"""
    num = 0.0
    den = 0
    for avg, count in pairs:
        if count <= 0:
            continue
        num += float(avg) * count
        den += count
    if den == 0:
        return None
    return round((num / den) / 10.0, 3)


def _status(mastery, attempts):
    if mastery is None or attempts == 0:
        return "pending", "待学习"
    if mastery >= MASTERED_AT:
        return "mastered", "已掌握"
    if mastery >= NEEDS_WORK_AT:
        return "improve", "需加强"
    return "weak", "薄弱"


def build_learning_path(domain_stats, due_cards=0, total_submissions=0):
    """
    由统计生成学习路径。

    domain_stats: [{"domain": str, "avg_score": float, "count": int}, ...]
                  —— 与 /api/stats 返回的 domain_stats 同结构
    due_cards:    今天待复习的 SM-2 卡片数
    total_submissions: 总作答次数（用于"还没开始"的整体提示）
    """
    by_domain = {d["domain"]: d for d in (domain_stats or []) if d.get("domain")}

    stages = []
    for st in STAGES:
        used = [by_domain[d] for d in st["domains"] if d in by_domain]
        attempts = sum(int(u.get("count") or 0) for u in used)
        mastery = _weighted_mastery(
            [(u.get("avg_score") or 0, int(u.get("count") or 0)) for u in used]
        )
        code, label = _status(mastery, attempts)
        stages.append({
            "key": st["key"],
            "name": st["name"],
            "desc": st["desc"],
            "domains": [d for d in st["domains"] if d in by_domain],
            "attempts": attempts,
            "mastery": mastery,          # 0-1，None = 没练过
            "status": code,              # pending / weak / improve / mastered
            "status_label": label,
        })

    # 下一步建议：优先级 = 状态（pending 与 weak 优先）+ 掌握度低
    priority = {"weak": 0, "pending": 1, "improve": 2, "mastered": 3}
    ranked = sorted(
        stages,
        key=lambda s: (priority[s["status"]], s["mastery"] if s["mastery"] is not None else 0.0),
    )
    focus = ranked[0] if ranked else None

    actions = []
    if total_submissions == 0:
        actions.append("还没有任何作答记录。从「阶段一：基础信息收集」的靶机开始，先建立做推演的习惯。")
    elif focus and focus["status"] in ("weak", "pending"):
        if focus["status"] == "pending":
            actions.append(f"「{focus['name']}」还没有练过，建议先挑一台这个方向的靶机。")
        else:
            actions.append(f"「{focus['name']}」目前掌握度 {focus['mastery']:.0%}，是最薄弱的环节，优先补。")
    if due_cards > 0:
        actions.append(f"今天有 {due_cards} 张错题卡片到期，先做复盘（SM-2 复习）再练新靶机，性价比最高。")
    if not actions:
        actions.append("四个阶段都已达到掌握线，可以去挑战更高难度（Hard）或没做过的 Domain。")

    return {
        "stages": stages,
        "focus_stage": focus["key"] if focus else None,
        "focus_name": focus["name"] if focus else None,
        "due_cards": int(due_cards or 0),
        "total_submissions": int(total_submissions or 0),
        "actions": actions,
    }
