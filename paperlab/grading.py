#!/usr/bin/env python3
# -*- coding: utf-8 -*-
#
# Project: PaperLab - AI Automated OSCP Lab Generator
# Author: tw1t
# Copyright 2026 tw1t
# SPDX-License-Identifier: Apache-2.0
"""
判卷引擎的提示词与结果解析。

从这里抽出来的原因有两个：

1. **benchmark 必须复用完全相同的提示词**，否则测出来的不是线上那个考官
   （改一个字的措辞都可能让评分分布漂移，测出来的稳定性就没有意义了）。
2. main.py 已经很长了，判卷这块逻辑（提示词 + 解析 + 校验）本来就自成一体。

这个模块不读配置、不连数据库 —— 纯函数，可以被 benchmark 直接调用。
"""

import json
import re

# 评分范围
SCORE_MIN = 0
SCORE_MAX = 10

GRADER_SYSTEM_PROMPT = """
        # Role
        你是一位极度挑剔、技术深厚的 OSCP 资深考官。

        # Task
        你将获得该靶机的 [终端日志(情报)]、[考核问题]、[考官底牌] 以及学生的 [推演作答]。
        请仔细比对学生是否从【终端日志】中精准提取了线索，并推理出可用的攻击链。

        # 打分尺度（比语气重要，请严格遵守）
        - **扣分必须有具体依据**：指出他漏了哪个线索、哪一步断了。没有依据就不扣分。
        - 方向正确但细节没落地（比如说了"从 SMB 拿凭证"但没指出具体文件）→ 给 **4-6 分**，
          这是"知道往哪走"，不是"做对了"。
        - 只写了泛泛思路、没有引用任何情报细节 → **2-3 分**。
        - 完全跑偏、或凭想象套用不存在的漏洞（情报里没有的 CVE/服务）→ **0-2 分**。
        - 完整、有依据、链条闭合 → **7-10 分**。

        # 替代路径（重要）
        学生走的路径**不必与考官底牌逐字一致**。只要他用的线索真实存在于终端日志、
        推理链自洽、且确实能达成目标（拿到立足点或提权），就应当按完成度正常给分，
        **绝不能因为"和底牌不一样"就扣分**。

        # 语气
        语气可以严厉、专业、一针见血，但**分数必须与完成度匹配** ——
        不要用语气来代替扣分理由。

        # Output Format (Strict JSON)
        必须严格输出 JSON 格式。
        {
            "evaluation_report": {
                "executive_summary": "总体评价（语气要硬核、专业、极其毒舌，一针见血指出致命失误）",
                "strengths": ["亮点"],
                "areas_for_improvement": ["技术短板"],
                "recommended_focus_domains": ["建议学习领域"]
            },
            "question_feedback": [
                {
                    "question_id": 1,
                    "score": 8,
                    "feedback": "具体的技术性评价。结合终端日志指出为何扣分，语气要严厉。",
                    "missed_key_insights": ["漏掉的核心名词，如：'SeImpersonatePrivilege', '未发现 .htpasswd 文件'"]
                }
            ]
        }
"""



# 中文模型写 JSON 时的高频坏习惯：全角标点、代码围栏、前后夹解释性文字。
# 这些都能**就地修**，没必要整次重调模型（一次调用是要花钱的）。
_FULLWIDTH_FIX = {
    "\uff0c": ",",   # ，
    "\uff1a": ":",   # ：
    "\u201c": '"',   # “
    "\u201d": '"',   # ”
    "\uff08": "(",   # （
    "\uff09": ")",   # ）
    "\uff1b": ";",   # ；
}


def _strip_fences(text):
    t = text.strip()
    if t.startswith("```"):
        nl = t.find("\n")
        if nl > 0:
            t = t[nl + 1:]
        if t.rstrip().endswith("```"):
            t = t.rstrip()[:-3]
    return t


def parse_json_tolerant(text):
    """
    尽量把一个"几乎合法"的 JSON 抠出来。失败才抛 ValueError。

    依次尝试：直接解析 → 去掉 Markdown 围栏 → 只取最外层花括号 →
    修全角标点。任何一步成功就返回，全部失败才放弃。
    """
    import json as _json
    import re as _re

    if isinstance(text, dict):
        return text
    if not isinstance(text, str) or not text.strip():
        raise ValueError("模型返回了空内容")

    base = _re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", "", text)
    candidates = [base, _strip_fences(base)]

    start, end = base.find("{"), base.rfind("}")
    if start >= 0 and end > start:
        candidates.append(base[start:end + 1])

    for cand in list(candidates):
        repaired = cand
        for bad, good in _FULLWIDTH_FIX.items():
            repaired = repaired.replace(bad, good)
        candidates.append(repaired)

    last_err = None
    for cand in candidates:
        try:
            return _json.loads(cand, strict=False)
        except Exception as e:  # noqa: BLE001 - 逐个候选试，最后统一报错
            last_err = e
    raise ValueError(f"模型返回的 JSON 无法解析: {last_err}")


def build_grading_prompts(context, questions, focus_points, student_writeup):
    """
    拼出判卷用的 (system_prompt, user_prompt)。

    questions / focus_points 可以是原始字符串（数据库里的），也可以是
    已解析的 list/dict —— benchmark 里用结构化数据更方便。
    """
    def _as_text(v):
        if isinstance(v, str):
            return v
        return json.dumps(v, ensure_ascii=False, indent=2)

    user_prompt = f"""
        # [The Battlefield (Terminal Logs - 学生看到的情报)]
        {_as_text(context)}

        # [The Questions (给学生的任务)]
        {_as_text(questions)}

        # [Hidden Rubric (考官底牌/预期路径)]
        {_as_text(focus_points)}

        # [Student's Technical Response (学生推演作答)]
        {student_writeup or "未提供内容"}
        """
    return GRADER_SYSTEM_PROMPT, user_prompt


def _clamp(score, lo=SCORE_MIN, hi=SCORE_MAX):
    try:
        s = float(score)
    except (TypeError, ValueError):
        return None
    if s != s:  # NaN
        return None
    return max(lo, min(hi, s))


def parse_grading_report(raw):
    """
    把模型返回的判卷 JSON 归一成可用的结构；不合法就抛 ValueError。

    为什么要有这一层：判卷结果会直接入库并参与排行榜聚合，
    一条 score 是字符串或 999 的脏数据会污染整张榜单（avg_score 是 SQL 聚合出来的）。
    """
    if isinstance(raw, str):
        text = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", "", raw)
        # 容忍模型在 JSON 前后加解释性文字
        start, end = text.find("{"), text.rfind("}")
        if start < 0 or end <= start:
            raise ValueError("判卷结果里找不到 JSON")
        try:
            report = json.loads(text[start:end + 1], strict=False)
        except json.JSONDecodeError as e:
            raise ValueError(f"判卷 JSON 解析失败: {e}")
    elif isinstance(raw, dict):
        report = raw
    else:
        raise ValueError("判卷结果类型不支持")

    fb = report.get("question_feedback")
    if not isinstance(fb, list) or not fb:
        raise ValueError("判卷结果缺少 question_feedback")

    cleaned = []
    for item in fb:
        if not isinstance(item, dict):
            continue
        score = _clamp(item.get("score"))
        if score is None:
            continue
        cleaned.append({
            "question_id": item.get("question_id"),
            "score": score,
            "feedback": str(item.get("feedback") or ""),
            "missed_key_insights": [str(x) for x in (item.get("missed_key_insights") or [])],
        })
    if not cleaned:
        raise ValueError("判卷结果里没有任何有效分数")
    report["question_feedback"] = cleaned
    return report


def average_score(report):
    """本次作答的平均分（与落库时算的 avg_score 保持一致）。"""
    scores = [q["score"] for q in report.get("question_feedback", [])
              if isinstance(q.get("score"), (int, float))]
    return round(sum(scores) / len(scores), 1) if scores else 0.0
