#!/usr/bin/env python3
# -*- coding: utf-8 -*-
#
# Project: PaperLab - AI Automated OSCP Lab Generator
# Author: tw1t
# Copyright 2026 tw1t
# SPDX-License-Identifier: Apache-2.0
"""
统计类路由：排行榜、连续打卡、个人统计。

排行榜刻意用 SQL 聚合（`GROUP BY operator_name` + `AVG(avg_score)`），
而不是把全部战报读进内存再解析 JSON —— 战报里是整份判卷文本，
几十个人就能堆出几十 MB，聚合放在 SQLite 里几乎是免费的。

连续打卡的两处细节：

- 日期按 **localtime** 归一（`date(timestamp, 'localtime')`），否则东八区
  凌晨做的题会被算到前一天。
- 连续起点允许是**今天或昨天**：昨天做了、今天还没做，不该显示连续中断。
"""

from datetime import date, timedelta
from typing import Optional

from fastapi import APIRouter, Header, Query

from ..deps import get_db_connection, guard_operator
from ..stats_service import compute_user_stats

router = APIRouter()


@router.get("/api/leaderboard")
async def get_leaderboard(limit: int = Query(20, ge=1, le=100)):
    """全平台排行榜：按平均分降序，平均分相同则按总次数降序。"""
    conn = get_db_connection()
    try:
        rows = conn.execute(
            """
            SELECT operator_name,
                   ROUND(AVG(avg_score), 1) AS avg_score,
                   COUNT(*) AS total_submissions
            FROM submissions
            WHERE avg_score > 0
            GROUP BY operator_name
            ORDER BY avg_score DESC, total_submissions DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
        return [{
            "rank": i,
            "operator_name": row["operator_name"],
            "avg_score": row["avg_score"],
            "total_submissions": row["total_submissions"],
        } for i, row in enumerate(rows, 1)]
    finally:
        conn.close()


@router.get("/api/streak")
async def get_streak(
    username: str,
    x_operator_pass: Optional[str] = Header(None, alias="X-Operator-Pass"),
):
    """返回用户连续打卡天数（当前连续 / 历史最长）。"""
    guard_operator(username, x_operator_pass)

    conn = get_db_connection()
    try:
        rows = conn.execute(
            "SELECT DISTINCT date(timestamp, 'localtime') AS day FROM submissions "
            "WHERE operator_name = ? ORDER BY day DESC",
            (username,),
        ).fetchall()
    finally:
        conn.close()

    days = [r["day"] for r in rows if r["day"]]
    if not days:
        return {"streak": 0, "longest": 0, "today": False}

    day_set = set(days)
    today = date.today()

    # 当天或昨天都算连续起点（今天还没做题时，昨天做了也算连续）
    check = today if today.isoformat() in day_set else today - timedelta(days=1)
    streak = 0
    while check.isoformat() in day_set:
        streak += 1
        check -= timedelta(days=1)

    longest = cur = 0
    prev = None
    for d in sorted(day_set):
        cur_d = date.fromisoformat(d)
        cur = cur + 1 if (prev and (cur_d - prev).days == 1) else 1
        longest = max(longest, cur)
        prev = cur_d

    return {"streak": streak, "longest": longest, "today": today.isoformat() in day_set}


@router.get("/api/stats")
async def get_stats(
    username: str,
    x_operator_pass: Optional[str] = Header(None, alias="X-Operator-Pass"),
):
    """返回用户个人统计数据：各 Domain 平均分、Tag 维度分析、总体趋势。

    计算逻辑在 stats_service，因为 /api/learning_path 也要用同一份统计。
    """
    guard_operator(username, x_operator_pass)
    return compute_user_stats(username)
