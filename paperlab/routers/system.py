#!/usr/bin/env python3
# -*- coding: utf-8 -*-
#
# Project: PaperLab - AI Automated OSCP Lab Generator
# Author: tw1t
# Copyright 2026 tw1t
# SPDX-License-Identifier: Apache-2.0
"""
系统路由：首页、健康检查、登录/注册。

分几次搬进来的（一次一条路由 + 一次跑测试），因为这个文件和 main.py 的配置
初始化纠缠在一起：必须先把 llm.py 抽出来，healthz 才能 import AI_MODEL
而不形成循环导入。

登录保留"口令保护是可选项"的设计：没设口令的代号直接放行（个人自用场景），
设了口令的才校验；老格式弱哈希（固定盐 SHA-256）在校验通过时顺手升级成 scrypt。
"""

import os

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse

from ..auth import AuthRequest, hash_password, needs_rehash, verify_password
from ..deps import get_db_connection
from ..llm import AI_MODEL

router = APIRouter()


@router.get("/")
async def serve_frontend():
    if os.path.exists("index.html"):
        return FileResponse("index.html")
    return {"error": "index.html not found"}

@router.get("/healthz")
async def healthz():
    """轻量健康检查，便于脚本/容器探活。"""
    try:
        conn = get_db_connection()
        try:
            n = conn.execute("SELECT COUNT(*) FROM labs").fetchone()[0]
        finally:
            conn.close()
        return {"status": "ok", "labs": n, "model": AI_MODEL}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Database Error: {str(e)}")

@router.post("/api/auth")
async def auth_operator(req: AuthRequest):
    """
    登录 / 首次注册代号。
    - 代号不存在 -> 立即注册（带了 password 就开启口令保护）
    - 已存在且未设口令 -> 直接放行
    - 已存在且设了口令 -> 校验 password
    """
    name = (req.name or "").strip()
    if not name:
        raise HTTPException(status_code=400, detail="代号不能为空")
    if len(name) > 32:
        raise HTTPException(status_code=400, detail="代号过长（最多 32 字符）")

    conn = get_db_connection()
    try:
        cursor = conn.cursor()
        row = cursor.execute(
            "SELECT pass_hash FROM operators WHERE name = ?", (name,)
        ).fetchone()

        if row is None:
            new_hash = hash_password(req.password) if req.password else None
            cursor.execute(
                "INSERT INTO operators (name, pass_hash) VALUES (?, ?)", (name, new_hash)
            )
            conn.commit()
            return {"status": "ok", "mode": "created", "protected": bool(new_hash)}

        stored = row["pass_hash"]
        if not stored:
            # 未设口令的代号：本次带了口令就借此启用保护（「自己给代号上锁」）。
            # 注意：任何人都能抢先锁住一个空闲代号；自建场景可接受，
            # 若需更强约束，请改为仅允许首次创建时设置。
            if req.password:
                cursor.execute(
                    "UPDATE operators SET pass_hash = ? WHERE name = ?",
                    (hash_password(req.password), name)
                )
                conn.commit()
                return {"status": "ok", "mode": "protected", "protected": True}
            return {"status": "ok", "mode": "open", "protected": False}
        if not req.password:
            raise HTTPException(status_code=401, detail="该代号已启用口令保护，请输入口令")
        if not verify_password(req.password, stored):
            raise HTTPException(status_code=401, detail="口令不正确")
        # 老格式（固定盐 SHA-256）校验通过后顺手升级成 scrypt —— 用户无感，
        # 下一次登录就自动换成强哈希了。不升级的话老账号会一直弱下去。
        if needs_rehash(stored):
            cursor.execute(
                "UPDATE operators SET pass_hash = ? WHERE name = ?",
                (hash_password(req.password), name),
            )
            conn.commit()
            log_line = "已把该代号的弱口令哈希升级为 scrypt"
            print(f"[auth] {log_line}（{name}）")
        return {"status": "ok", "mode": "verified", "protected": True}
    finally:
        conn.close()
