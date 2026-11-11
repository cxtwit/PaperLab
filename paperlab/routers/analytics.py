#!/usr/bin/env python3
# -*- coding: utf-8 -*-
#
# Project: PaperLab - AI Automated OSCP Lab Generator
# Author: tw1t
# Copyright 2026 tw1t
# SPDX-License-Identifier: Apache-2.0
"""
统计类路由：生成成本统计、个性化学习路径。

这是路由拆分的第一组，选它是因为这两条路由都是独立的新功能、
没有历史包袱 —— 先把"拆分后的路由长什么样"确定下来，再搬老路由。
"""

from typing import Optional

from fastapi import APIRouter, Header

from ..deps import get_db_connection, guard_operator
from lab_generator import DB_FILE, generation_stats_summary
from ..learning_path import build_learning_path
from ..stats_service import compute_user_stats

router = APIRouter()


@router.get("/api/generation_stats")
async def generation_stats():
    """
    生成成本与成功率统计。

    数据全部来自 generation_stats 表里真实采集的 usage —— 没有记录时返回全 0，
    不会编一个"看起来合理"的估值。这个接口回答的是"跑一批到底花了多少"。
    """
    return generation_stats_summary(DB_FILE)


@router.get("/api/learning_path")
async def learning_path_api(
    username: str,
    x_operator_pass: Optional[str] = Header(None, alias="X-Operator-Pass"),
):
    """
    个性化学习路径：把统计翻译成"下一步该练什么"。

    统计复用 stats_service.compute_user_stats（与 /api/stats 同一份口径）——
    两处各算一套的话，界面上迟早出现"统计页说 A、路径页说 B"。
    """
    guard_operator(username, x_operator_pass)
    stats = compute_user_stats(username)
    conn = get_db_connection()
    try:
        due = conn.execute(
            "SELECT COUNT(*) AS n FROM sm2_schedule"
            " WHERE operator_name = ? AND next_review <= date('now', 'localtime')",
            (username,),
        ).fetchone()["n"]
    finally:
        conn.close()
    return build_learning_path(
        stats.get("domain_stats", []),
        due_cards=due,
        total_submissions=stats.get("total", 0),
    )
