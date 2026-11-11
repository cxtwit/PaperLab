#!/usr/bin/env python3
# -*- coding: utf-8 -*-
#
# Project: PaperLab - AI Automated OSCP Lab Generator
# Author: tw1t
# Copyright 2026 tw1t
# SPDX-License-Identifier: Apache-2.0
"""
错题本路由：增 / 查 / 删 / 导出 Markdown。

导出的两个坑（都已处理）：

1. **认证**：接口要求 `X-Operator-Pass`，所以前端不能用 `<a href>` 直接下载
   （`<a>` 带不了请求头），必须 fetch + Blob。
2. **中文代号**：非 ASCII 文件名不能直接塞进响应头（ASGI 按 latin-1 编码会 500），
   按 RFC 5987 用 `filename*=UTF-8''` 提供，并保留 ASCII 兜底名。
"""

import json
from datetime import datetime
from typing import Optional
from urllib.parse import quote

from fastapi import APIRouter, Header, HTTPException
from fastapi.responses import Response
from pydantic import BaseModel

from ..deps import get_db_connection, guard_operator

router = APIRouter()


class BookmarkItem(BaseModel):
    username: str
    lab_id: str
    question_text: str
    question_focus: str
    missed_insights: list
    feedback: str
    score: int


def _parse_missed(raw):
    try:
        v = json.loads(raw)
        return v if isinstance(v, list) else []
    except (json.JSONDecodeError, TypeError):
        return []


@router.post("/api/bookmarks")
async def add_bookmark(
    item: BookmarkItem,
    x_operator_pass: Optional[str] = Header(None, alias="X-Operator-Pass"),
):
    """收藏一道错题到错题本"""
    guard_operator(item.username, x_operator_pass)
    conn = get_db_connection()
    try:
        cursor = conn.cursor()
        cursor.execute('''
            INSERT INTO bookmarks (operator_name, lab_id, question_text, question_focus,
                                   missed_insights, feedback, score)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        ''', (
            item.username, item.lab_id, item.question_text, item.question_focus,
            json.dumps(item.missed_insights, ensure_ascii=False), item.feedback, item.score
        ))
        conn.commit()
        return {"status": "ok", "id": cursor.lastrowid}
    finally:
        conn.close()


@router.get("/api/bookmarks")
async def get_bookmarks(
    username: str,
    x_operator_pass: Optional[str] = Header(None, alias="X-Operator-Pass"),
):
    """获取用户错题本"""
    guard_operator(username, x_operator_pass)
    conn = get_db_connection()
    try:
        rows = conn.execute(
            "SELECT * FROM bookmarks WHERE operator_name = ? ORDER BY timestamp DESC",
            (username,)
        ).fetchall()
        return [{
            "id": row["id"],
            "lab_id": row["lab_id"],
            "question_text": row["question_text"],
            "question_focus": row["question_focus"],
            "missed_insights": _parse_missed(row["missed_insights"]),
            "feedback": row["feedback"],
            "score": row["score"],
            "timestamp": row["timestamp"],
        } for row in rows]
    finally:
        conn.close()


@router.delete("/api/bookmarks/{bookmark_id}")
async def delete_bookmark(
    bookmark_id: int,
    username: str,
    x_operator_pass: Optional[str] = Header(None, alias="X-Operator-Pass"),
):
    """删除一条错题记录（只能删自己的）"""
    guard_operator(username, x_operator_pass)
    conn = get_db_connection()
    try:
        cursor = conn.cursor()
        cursor.execute(
            "DELETE FROM bookmarks WHERE id = ? AND operator_name = ?",
            (bookmark_id, username)
        )
        conn.commit()
        return {"status": "ok"}
    finally:
        conn.close()


@router.get("/api/bookmarks/export")
async def export_bookmarks(
    username: str,
    x_operator_pass: Optional[str] = Header(None, alias="X-Operator-Pass"),
):
    """将用户错题本导出为 Markdown 格式文本"""
    guard_operator(username, x_operator_pass)
    conn = get_db_connection()
    try:
        rows = conn.execute(
            "SELECT * FROM bookmarks WHERE operator_name = ? ORDER BY timestamp DESC",
            (username,)
        ).fetchall()
        if not rows:
            raise HTTPException(status_code=404, detail="错题本为空")

        lines = [
            f"# PaperLab 错题本 — {username}",
            f"> 导出时间：{datetime.now().strftime('%Y-%m-%d %H:%M')}",
            f"> 共 {len(rows)} 条错题记录",
            "",
        ]
        for i, row in enumerate(rows, 1):
            missed = _parse_missed(row["missed_insights"])
            lines += [
                "---",
                f"## {i}. {row['question_text']}",
                "",
                f"**靶机**：`{row['lab_id']}`　　**得分**：{row['score']}/10　　**时间**：{row['timestamp']}",
                "",
                f"**考点 Focus**：{row['question_focus']}",
                "",
                "**AI 点评**：",
                "",
                f"> {row['feedback']}",
                "",
            ]
            if missed:
                lines.append("**遗漏的核心知识点**：")
                for m in missed:
                    lines.append(f"- `{m}`")
                lines.append("")

        md_content = "\n".join(lines)

        # 中文/非 ASCII 代号不能直接进响应头（ASGI 按 latin-1 编码会 500）。
        ascii_fallback = "paperlab_wrongbook.md"
        utf8_name = quote(f"paperlab_wrongbook_{username}.md", safe="")
        disposition = (
            f'attachment; filename="{ascii_fallback}"; filename*=UTF-8\'\'{utf8_name}'
        )
        return Response(
            content=md_content.encode("utf-8"),
            media_type="text/markdown; charset=utf-8",
            headers={"Content-Disposition": disposition}
        )
    finally:
        conn.close()
