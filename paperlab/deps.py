#!/usr/bin/env python3
# -*- coding: utf-8 -*-
#
# Project: PaperLab - AI Automated OSCP Lab Generator
# Author: tw1t
# Copyright 2026 tw1t
# SPDX-License-Identifier: Apache-2.0
"""
路由共享依赖：数据库连接、JSON 容错、口令校验。

为什么单独一个模块：把路由按主题拆开之后，每个路由模块都需要这几样东西。
如果它们还留在 main.py 里，路由模块就得 `import main` —— 而 main 又要
`import routers` 来注册，直接循环导入。抽到 deps.py 后依赖方向是单向的：

    lab_generator ← deps ← routers ← main

main.py 里保留同名别名，所以没被搬走的路由照旧能用。
"""

import json

from fastapi import HTTPException

from .auth import verify_password
from lab_generator import DB_FILE


def get_db_connection():
    """每次请求一个连接（WAL 模式，无连接池）。"""
    import sqlite3
    conn = sqlite3.connect(DB_FILE, timeout=30)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.row_factory = sqlite3.Row
    return conn


def safe_json(raw, default):
    """历史数据里可能存在非法 JSON，解析失败时降级为默认值而不是抛 500。"""
    try:
        return json.loads(raw) if raw else default
    except (json.JSONDecodeError, TypeError):
        return default


def lookup_pass_hash(name: str):
    conn = get_db_connection()
    try:
        row = conn.execute(
            "SELECT pass_hash FROM operators WHERE name = ?", (name,)
        ).fetchone()
        return row["pass_hash"] if row else None
    finally:
        conn.close()


def guard_operator(username: str, provided_pass):
    """
    校验某代号是否有权操作。

    未注册 / 未设口令 → 放行（向后兼容，口令保护是"想开才开"的可选项）；
    设了口令 → 必须匹配。
    """
    stored = lookup_pass_hash(username)
    if not stored:
        return username
    if not provided_pass or not verify_password(provided_pass, stored):
        raise HTTPException(status_code=403, detail="该代号已启用了口令保护，凭证不正确")
    return username
