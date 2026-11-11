#!/usr/bin/env python3
# -*- coding: utf-8 -*-
#
# Project: PaperLab - AI Automated OSCP Lab Generator
# Author: tw1t
# Copyright 2026 tw1t
# SPDX-License-Identifier: Apache-2.0
"""
历史战报查询（分页）。

两个细节：
- 损坏的历史记录（report 不是合法 JSON）**跳过而不是整体 500** ——
  一条脏数据不该让整页战报打不开。
- `summary` 只截前 50 字：列表页用不到全文，而完整判卷文本可能很长，
  但 `report` 字段仍然返回全文（编辑/复盘要用）。
"""

import json
from typing import Optional

from fastapi import APIRouter, Header, Query

from ..deps import get_db_connection, guard_operator

router = APIRouter()


@router.get("/api/history")
async def get_history(
    username: str,
    page: int = Query(1, ge=1),
    page_size: int = Query(15, ge=1, le=100),
    x_operator_pass: Optional[str] = Header(None, alias="X-Operator-Pass"),
):
    """带平均分勋章的历史战报查询，支持分页，且仅拉取当前用户的记录"""
    guard_operator(username, x_operator_pass)
    conn = get_db_connection()
    try:
        cursor = conn.cursor()
        cursor.execute("SELECT COUNT(*) FROM submissions WHERE operator_name = ?", (username,))
        total = cursor.fetchone()[0]

        offset = (page - 1) * page_size
        cursor.execute(
            "SELECT * FROM submissions WHERE operator_name = ? "
            "ORDER BY timestamp DESC LIMIT ? OFFSET ?",
            (username, page_size, offset)
        )
        rows = cursor.fetchall()

        history_list = []
        for row in rows:
            try:
                report = json.loads(row["report"])
            except (json.JSONDecodeError, TypeError):
                continue  # 跳过损坏的历史记录，而不是让整页 500
            scores = [q["score"] for q in report.get("question_feedback", [])]
            avg = round(sum(scores) / len(scores), 1) if scores else 0
            summary_raw = report.get("evaluation_report", {}).get("executive_summary", "")
            history_list.append({
                "id": row["id"],
                "lab_id": row["lab_id"],
                "timestamp": row["timestamp"],
                "avg_score": avg,
                "summary": summary_raw[:50] + "..." if len(summary_raw) > 50 else summary_raw,
                "report": report,
            })
        return {"items": history_list, "total": total,
                "page": page, "page_size": page_size}
    finally:
        conn.close()
