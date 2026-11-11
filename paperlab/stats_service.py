#!/usr/bin/env python3
# -*- coding: utf-8 -*-
#
# Project: PaperLab - AI Automated OSCP Lab Generator
# Author: tw1t
# Copyright 2026 tw1t
# SPDX-License-Identifier: Apache-2.0
"""
用户统计计算（纯查询 + 聚合）。

从 `/api/stats` 里抽出来的原因：`/api/learning_path` 也要用同一份统计。
留在路由里的话，学习路径那个路由只能去 `import main` 调另一个路由函数 ——
既绕又容易把两边的口径改分叉。
"""

import json

from .deps import get_db_connection


def compute_user_stats(username: str) -> dict:
    """
    单个代号的作答统计：总分、各 Domain / Tag 平均分、趋势。

    avg_score 逐条由 question_feedback 现算（不读库里缓存的 avg_score），
    免得历史数据的缓存值不一致时两份数字对不上。
    """
    conn = get_db_connection()
    try:
        cursor = conn.cursor()

        total = cursor.execute(
            "SELECT COUNT(*) AS total FROM submissions WHERE operator_name = ?",
            (username,)
        ).fetchone()["total"]
        if total == 0:
            return {"total": 0, "avg_score": 0, "domain_stats": [],
                    "tag_stats": [], "trend": []}

        rows = cursor.execute(
            "SELECT s.lab_id, s.report, s.timestamp, l.domain, l.tags "
            "FROM submissions s LEFT JOIN labs l ON s.lab_id = l.id "
            "WHERE s.operator_name = ? ORDER BY s.timestamp ASC",
            (username,)
        ).fetchall()

        domain_data, tag_data, trend, all_avgs = {}, {}, [], []

        for row in rows:
            try:
                report = json.loads(row["report"])
            except (json.JSONDecodeError, TypeError):
                continue
            scores = [q["score"] for q in report.get("question_feedback", [])
                      if isinstance(q, dict) and isinstance(q.get("score"), (int, float))]
            if not scores:
                continue
            avg = round(sum(scores) / len(scores), 1)
            all_avgs.append(avg)

            domain = row["domain"] or "Unknown"
            domain_data.setdefault(domain, []).append(avg)

            try:
                tags = json.loads(row["tags"]) if row["tags"] else []
                if not isinstance(tags, list):
                    tags = []
            except (json.JSONDecodeError, TypeError):
                tags = []
            for tag in tags:
                tag_data.setdefault(tag, []).append(avg)

            trend.append({"timestamp": row["timestamp"], "avg_score": avg,
                          "lab_id": row["lab_id"]})

        global_avg = round(sum(all_avgs) / len(all_avgs), 1) if all_avgs else 0

        domain_stats = sorted([
            {"domain": d, "avg_score": round(sum(v) / len(v), 1), "count": len(v)}
            for d, v in domain_data.items()
        ], key=lambda x: x["avg_score"])

        tag_stats = sorted([
            {"tag": t, "avg_score": round(sum(v) / len(v), 1), "count": len(v)}
            for t, v in tag_data.items()
        ], key=lambda x: x["avg_score"])

        return {
            "total": total,
            "avg_score": global_avg,
            "domain_stats": domain_stats,
            "tag_stats": tag_stats,
            "trend": trend,
        }
    finally:
        conn.close()
