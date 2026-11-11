#!/usr/bin/env python3
# -*- coding: utf-8 -*-
#
# Project: PaperLab - AI Automated OSCP Lab Generator
# Author: tw1t
# Copyright 2026 tw1t
# SPDX-License-Identifier: Apache-2.0
"""
判卷与提示路由。

两条路由共用同一套"计费调用只重试模型本身"的策略：重试不会连带任何写库操作，
写库失败也只重试写库。

**这个文件是从 main.py 整块搬出来的（连带 student/hint 的请求模型）。**
搬的理由不只是"main 太长"——`grade_once` 在之前搬 get_lab 时被误删过，
而测试没有一处真正调用 /api/evaluate，所以那个 NameError 一直潜伏到运行时。
搬出来之后顺手补了一条会真调这条路由的测试。
"""

import asyncio
import functools
import json
import time
from typing import Optional

from fastapi import APIRouter, Header, HTTPException
from pydantic import BaseModel

from .. import grading
from ..deps import get_db_connection, guard_operator
from ..deps import safe_json as _safe_json
from lab_generator import (
    _disable_json_mode,
    _json_mode_arg,
    _response_format_rejected,
    clean_and_parse_json,
)
from ..llm import AI_MODEL, client

router = APIRouter()


def grade_once(system_prompt, user_prompt):
    """
    调用阅卷模型并解析结果。

    只重试「模型调用 + JSON 解析」这两步（它们计费但重试有意义），
    不包含任何持久化操作 —— 否则写库失败会连带重调一次计费模型。
    """
    last_error = "未知错误"
    raw = ""
    attempts = 0
    while attempts < 3:
        kwargs = {
            "model": AI_MODEL,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt}
            ],
            "temperature": 0.1,  # 低温保证评分一致性
            # 判卷要输出「总评 + 每题反馈」，3000 有被截断的风险（截断的 JSON 只能整次重调）。
            "max_tokens": 4000,
        }
        kwargs.update(_json_mode_arg())
        try:
            response = client.chat.completions.create(**kwargs)
            raw = response.choices[0].message.content
            # 用容错解析而不是严格 json.loads：中文模型常输出全角标点、
            # 代码围栏或前后夹带解释文字，这些能就地修，不必再花一次调用。
            return grading.parse_json_tolerant(raw), None
        except (json.JSONDecodeError, ValueError):
            attempts += 1
            last_error = "AI 返回了无效的成绩单格式"
        except Exception as e:
            if _response_format_rejected(e) and _disable_json_mode():
                continue
            attempts += 1
            last_error = f"AI 判卷通信故障: {str(e)}"
    return None, last_error


class StudentSubmission(BaseModel):
    lab_id: str
    username: str
    answers: dict


# ==========================================
# 4. 业务路由 API
# ==========================================

@router.post("/api/evaluate")
async def evaluate_submission(
    submission: StudentSubmission,
    x_operator_pass: Optional[str] = Header(None, alias="X-Operator-Pass"),
):
    """判卷引擎：全知全能的毒舌考官 + 异常防线"""
    guard_operator(submission.username, x_operator_pass)

    conn = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor()

        cursor.execute(
            "SELECT context, questions, focus_points FROM labs WHERE id = ?",
            (submission.lab_id,)
        )
        lab_data = cursor.fetchone()

        if not lab_data:
            raise HTTPException(status_code=404, detail="靶机未找到")

        student_writeup = submission.answers.get("student_writeup", "未提供内容")

        # 判卷提示词与解析统一放在 grading.py：
        # benchmark（grading_benchmark.py）必须复用**完全相同**的提示词，
        # 否则测出来的不是线上这个考官。
        system_prompt, user_prompt = grading.build_grading_prompts(
            lab_data["context"], lab_data["questions"],
            lab_data["focus_points"], student_writeup
        )

        # 放到线程池执行，避免阻塞事件循环
        loop = asyncio.get_running_loop()
        ai_report, grade_error = await loop.run_in_executor(
            None, functools.partial(grade_once, system_prompt, user_prompt)
        )
        if ai_report is None:
            raise HTTPException(status_code=502, detail=grade_error or "AI 判卷失败")
        try:
            # 归一：分数夹紧到 0-10、丢掉没有分数的条目。
            # 判卷结果会直接参与排行榜的 SQL 聚合，一条 999 分就能污染整张榜。
            ai_report = grading.parse_grading_report(ai_report)
        except ValueError as e:
            raise HTTPException(status_code=502, detail=f"AI 判卷结果无法解析: {e}")

        # 落库时同步算出本次平均分，供排行榜直接 SQL 聚合，无需事后解析 JSON
        submission_avg = grading.average_score(ai_report)

        cursor.execute('''
            INSERT INTO submissions (lab_id, operator_name, student_writeup, report, avg_score)
            VALUES (?, ?, ?, ?, ?)
        ''', (submission.lab_id, submission.username, student_writeup,
              json.dumps(ai_report, ensure_ascii=False), submission_avg))
        conn.commit()

        return ai_report

    except HTTPException:
        raise
    except Exception as e:
        print(f"判卷异常: {e}")
        raise HTTPException(status_code=500, detail=f"AI 判卷通信故障: {str(e)}")
    finally:
        if conn:
            conn.close()



class HintRequest(BaseModel):
    lab_id: str
    question_idx: int
    username: Optional[str] = None


HINT_SYSTEM_PROMPT = """你是 OSCP 考官助手。学生请求提示。
你的任务：从考点中，为指定题目提炼一个【方向性提示】。
规则：
1. 只给方向，绝对不给具体漏洞名/CVE/命令/路径。
2. 提示必须是中文，1-2 句话，简洁有力。
3. 可以引导学生思考"应该关注情报的哪一部分"，但不能直接说"漏洞是X"。
4. 只输出纯文本提示内容，无需 JSON 包裹。"""


def _hint_sync(question_text, question_focus, focus_points):
    """同步生成方向提示；由线程池调用，避免阻塞事件循环。"""
    user_prompt = (
        f"题目：{question_text}\n"
        f"考点（仅供你参考，绝对不能泄露）：{question_focus}\n"
        f"全局考点：{focus_points}\n\n"
        "请输出一条对学生的方向性提示："
    )
    last_error = "未知错误"
    for attempt in range(3):
        try:
            resp = client.chat.completions.create(
                model=AI_MODEL,
                messages=[
                    {"role": "system", "content": HINT_SYSTEM_PROMPT},
                    {"role": "user", "content": user_prompt},
                ],
                temperature=0.4,
                max_tokens=200,
            )
            return (resp.choices[0].message.content or "").strip(), None
        except Exception as e:
            last_error = str(e)
            if attempt < 2:
                time.sleep(2 ** attempt)
    return None, last_error


@router.post("/api/hint")
async def get_hint(
    req: HintRequest,
    x_operator_pass: Optional[str] = Header(None, alias="X-Operator-Pass"),
):
    """为指定题目生成方向提示（只给方向，不泄露答案）。"""
    # 【这里以前固定传 None】前端明明带了 X-Operator-Pass，后端却忽略了它，
    # 于是启用口令保护的用户每次请求提示都会被判 403 —— 提示功能对这部分用户
    # 等于不存在（正是审查报告点出的问题）。现在把请求头接进来。
    if req.username:
        guard_operator(req.username, x_operator_pass)

    conn = get_db_connection()
    try:
        row = conn.execute(
            "SELECT questions, focus_points FROM labs WHERE id = ?", (req.lab_id,)
        ).fetchone()
    finally:
        conn.close()

    if not row:
        raise HTTPException(status_code=404, detail="靶机未找到")

    questions = _safe_json(row["questions"], [])
    if req.question_idx < 0 or req.question_idx >= len(questions):
        raise HTTPException(status_code=400, detail="题目编号超出范围")

    q = questions[req.question_idx]
    loop = asyncio.get_running_loop()
    hint, err = await loop.run_in_executor(
        None,
        functools.partial(_hint_sync, q.get("text", ""), q.get("focus", ""), row["focus_points"]),
    )
    if hint is None:
        raise HTTPException(status_code=503, detail=f"提示生成失败：{err}")
    return {"hint": hint}


# ==========================================
# 5. 错题本 API
# ==========================================

# ==========================================
# 6. MD 文件上传 → 实时裂变生成（SSE）
# ==========================================
