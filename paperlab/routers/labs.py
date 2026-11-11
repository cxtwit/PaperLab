#!/usr/bin/env python3
# -*- coding: utf-8 -*-
#
# Project: PaperLab - AI Automated OSCP Lab Generator
# Author: tw1t
# Copyright 2026 tw1t
# SPDX-License-Identifier: Apache-2.0
"""
靶机库路由：列表、筛选项、详情、已做列表。

**安全要点**：`/api/get_lab` 是学生唯一能拿到靶机内容的入口，
`focus_points`（评分要点）和 `attack_chain`（结构化攻击链）都是答案键，
绝不能出现在这里 —— 查询用列白名单而不是 SELECT *，并且有专门的测试
（tests/test_answer_key_leak.py）盯着响应里没有这些字段。
"""

from typing import Optional

from fastapi import APIRouter, Header, HTTPException, Query

from ..deps import get_db_connection, guard_operator
from ..deps import safe_json as _safe_json
from lab_generator import ALLOWED_DIFFICULTIES, ALLOWED_DOMAINS

router = APIRouter()


@router.get("/api/list_labs")
async def list_labs(
    os_filter: Optional[str] = Query(None, alias="os"),
    domain: Optional[str] = Query(None),
    difficulty: Optional[str] = Query(None),
):
    try:
        conn = get_db_connection()
        try:
            conditions, params = [], []
            if os_filter:
                conditions.append("os = ?")
                params.append(os_filter)
            if domain:
                conditions.append("domain = ?")
                params.append(domain)
            if difficulty:
                conditions.append("difficulty = ?")
                params.append(difficulty)

            where_clause = ("WHERE " + " AND ".join(conditions)) if conditions else ""
            rows = conn.execute(
                f"SELECT id, os, difficulty, domain FROM labs {where_clause}", params
            ).fetchall()
            return [dict(row) for row in rows]
        finally:
            conn.close()
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Database Error: {str(e)}")


@router.get("/api/filters")
async def get_filters():
    """返回规范的筛选项，供前端下拉使用（避免被脏数据污染出十几个碎片选项）。"""
    return {"domains": ALLOWED_DOMAINS, "difficulties": ALLOWED_DIFFICULTIES}


@router.get("/api/get_lab/{lab_id}")
async def get_lab_detail(lab_id: str):
    conn = get_db_connection()
    try:
        # 显式列白名单：focus_points / attack_chain 是答案键，绝不能出现在这里。
        row = conn.execute(
            "SELECT id, os, difficulty, domain, tags, context, questions"
            " FROM labs WHERE id = ?", (lab_id,)
        ).fetchone()
    finally:
        conn.close()

    if not row:
        raise HTTPException(status_code=404, detail="靶机未找到")

    return {
        "id": row["id"],
        "os": row["os"],
        "difficulty": row["difficulty"],
        "domain": row["domain"],
        "tags": _safe_json(row["tags"], []),
        "context": row["context"],
        "questions": _safe_json(row["questions"], []),
    }


@router.get("/api/done_labs")
async def get_done_labs(
    username: str,
    x_operator_pass: Optional[str] = Header(None, alias="X-Operator-Pass"),
):
    """返回该用户做过的所有靶机 id 列表（用于前端完整排除已做）"""
    guard_operator(username, x_operator_pass)
    conn = get_db_connection()
    try:
        rows = conn.execute(
            "SELECT DISTINCT lab_id FROM submissions WHERE operator_name = ?",
            (username,)
        ).fetchall()
        return [row["lab_id"] for row in rows]
    finally:
        conn.close()
