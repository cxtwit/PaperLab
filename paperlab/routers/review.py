#!/usr/bin/env python3
# -*- coding: utf-8 -*-
#
# Project: PaperLab - AI Automated OSCP Lab Generator
# Author: tw1t
# Copyright 2026 tw1t
# SPDX-License-Identifier: Apache-2.0
"""
SM-2 间隔重复路由：提交复盘评分、今日到期卡片、整体进度。

两个容易踩的细节，注释保留在实现里：

1. **用 localtime 而不是 UTC**：`date('now')` 是 UTC，会让北京时间 00:00–08:00
   的"今日复习"取不到当天卡片。
2. **前端传 0-10、后端折算 0-5**：前端只有"没做出来 / 模糊 / 掌握"三档（2/6/10），
   除以 2 得到 SM-2 的 1/3/5。
"""

import json
from typing import Optional

from fastapi import APIRouter, Header
from pydantic import BaseModel

from ..deps import get_db_connection, guard_operator

router = APIRouter()

SM2_MAX_INTERVAL = 180


class SM2ReviewItem(BaseModel):
    username: str
    lab_id: str
    question_idx: int
    question_text: str
    score_10: int  # 前端传 0-10 的原始分，后端折算为 SM-2 的 0-5


def sm2_update(easiness: float, interval: int, repetitions: int, score: int):
    """
    SM-2 算法核心计算。
    score: 0-5
    返回: (new_easiness, new_interval, new_repetitions)
    """
    if score < 3:
        # 答错，重置
        repetitions = 0
        interval = 1
    else:
        if repetitions == 0:
            interval = 1
        elif repetitions == 1:
            interval = 6
        else:
            interval = round(interval * easiness)
        repetitions += 1

    interval = max(1, min(int(interval), SM2_MAX_INTERVAL))
    easiness = max(1.3, easiness + 0.1 - (5 - score) * (0.08 + (5 - score) * 0.02))
    return round(easiness, 2), interval, repetitions


@router.post("/api/sm2/review")
async def sm2_review(
    item: SM2ReviewItem,
    x_operator_pass: Optional[str] = Header(None, alias="X-Operator-Pass"),
):
    """提交一道题的复盘评分，更新 SM-2 调度"""
    guard_operator(item.username, x_operator_pass)
    score_10 = max(0, min(10, int(item.score_10)))
    score_5 = round(score_10 / 2)  # 10 分制 → 5 分制
    conn = get_db_connection()
    try:
        cursor = conn.cursor()
        row = cursor.execute(
            "SELECT easiness, interval, repetitions FROM sm2_schedule "
            "WHERE operator_name=? AND lab_id=? AND question_idx=?",
            (item.username, item.lab_id, item.question_idx)
        ).fetchone()
        easiness = row["easiness"] if row else 2.5
        interval = row["interval"] if row else 1
        repetitions = row["repetitions"] if row else 0

        new_e, new_i, new_r = sm2_update(easiness, interval, repetitions, score_5)

        # date('now') 是 UTC：会让北京时间 00:00–08:00 的「今日复习」取不到当天卡片，
        # 所以必须用 localtime。
        cursor.execute('''
            INSERT INTO sm2_schedule (operator_name, lab_id, question_idx, question_text,
                                      easiness, interval, repetitions, next_review, last_score)
            VALUES (?, ?, ?, ?, ?, ?, ?, date('now', 'localtime', ? || ' days'), ?)
            ON CONFLICT(operator_name, lab_id, question_idx) DO UPDATE SET
                easiness=excluded.easiness,
                interval=excluded.interval,
                repetitions=excluded.repetitions,
                next_review=excluded.next_review,
                last_score=excluded.last_score
        ''', (
            item.username, item.lab_id, item.question_idx, item.question_text,
            new_e, new_i, new_r, str(new_i), score_10
        ))
        conn.commit()
        return {"status": "ok", "next_review_in_days": new_i,
                "easiness": new_e, "repetitions": new_r}
    finally:
        conn.close()


@router.get("/api/sm2/today")
async def sm2_today(
    username: str,
    x_operator_pass: Optional[str] = Header(None, alias="X-Operator-Pass"),
):
    """返回今日需要复盘的题目列表（next_review <= today），含对应考点 focus"""
    guard_operator(username, x_operator_pass)
    conn = get_db_connection()
    try:
        rows = conn.execute('''
            SELECT s.id, s.lab_id, s.question_idx, s.question_text,
                   s.easiness, s.interval, s.repetitions, s.next_review, s.last_score,
                   l.os, l.difficulty, l.domain, l.questions AS questions_json
            FROM sm2_schedule s
            LEFT JOIN labs l ON s.lab_id = l.id
            WHERE s.operator_name = ? AND s.next_review <= date('now', 'localtime')
            ORDER BY s.next_review ASC
        ''', (username,)).fetchall()
        result = []
        for row in rows:
            card = dict(row)
            try:
                questions = json.loads(card.pop("questions_json") or "[]")
                idx = card["question_idx"]
                card["question_focus"] = questions[idx]["focus"] if idx < len(questions) else ""
            except Exception:
                card["question_focus"] = ""
            result.append(card)
        return result
    finally:
        conn.close()


@router.get("/api/sm2/stats")
async def sm2_stats(
    username: str,
    x_operator_pass: Optional[str] = Header(None, alias="X-Operator-Pass"),
):
    """返回用户 SM-2 整体进度统计"""
    guard_operator(username, x_operator_pass)
    conn = get_db_connection()
    try:
        cursor = conn.cursor()
        total = cursor.execute(
            "SELECT COUNT(*) AS total FROM sm2_schedule WHERE operator_name=?", (username,)
        ).fetchone()["total"]
        due = cursor.execute(
            "SELECT COUNT(*) AS due FROM sm2_schedule "
            "WHERE operator_name=? AND next_review <= date('now', 'localtime')", (username,)
        ).fetchone()["due"]
        avg_row = cursor.execute(
            "SELECT AVG(last_score) AS avg_score FROM sm2_schedule WHERE operator_name=?",
            (username,)
        ).fetchone()
        avg_score = round(avg_row["avg_score"] or 0, 1)
        return {"total_cards": total, "due_today": due, "avg_last_score": avg_score}
    finally:
        conn.close()
