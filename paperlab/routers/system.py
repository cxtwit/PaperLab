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
from typing import Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
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

    **登录只做认证，不设口令。** 设口令是登录之后在设置里做的（/api/operator/password）。
    以前这里会"带了口令就顺手给代号上锁"，同一个输入框按状态分别表示
    创建 / 上锁 / 密码校验三件事 —— 而且任何人抢先给一个空闲代号带上口令，
    就能把别人挡在外面。现在这条路去掉了。

    - 代号不存在 -> 创建（不带口令）
    - 已存在且未设口令 -> 直接放行
    - 已存在且设了口令 -> 校验口令
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
            cursor.execute(
                "INSERT INTO operators (name, pass_hash) VALUES (?, NULL)", (name,)
            )
            conn.commit()
            return {"status": "ok", "mode": "created", "protected": False}

        stored = row["pass_hash"]
        if not stored:
            return {"status": "ok", "mode": "open", "protected": False}
        if not req.password:
            raise HTTPException(status_code=401, detail="该代号已启用口令保护，请输入口令")
        if not verify_password(req.password, stored):
            raise HTTPException(status_code=401, detail="口令不正确")
        # 老格式（固定盐 SHA-256）校验通过后顺手升级成 scrypt。
        if needs_rehash(stored):
            cursor.execute(
                "UPDATE operators SET pass_hash = ? WHERE name = ?",
                (hash_password(req.password), name),
            )
            conn.commit()
            print(f"[auth] 已把该代号的弱口令哈希升级为 scrypt（{name}）")
        return {"status": "ok", "mode": "verified", "protected": True}
    finally:
        conn.close()


class PasswordChange(BaseModel):
    username: str
    current_password: Optional[str] = None   # 已设口令时必填
    new_password: Optional[str] = None       # 留空 = 取消口令保护


@router.post("/api/operator/password")
async def set_operator_password(req: PasswordChange):
    """
    设置 / 修改 / 取消代号口令（登录之后用）。

    规则：
    - 代号已经设了口令 -> 必须先提供当前口令，才能修改或取消
    - 代号没设口令 -> 可以直接设置（这是它的"上锁"动作）
    - new_password 留空 -> 取消保护，之后该代号谁能用
    """
    name = (req.username or "").strip()
    if not name:
        raise HTTPException(status_code=400, detail="代号不能为空")

    conn = get_db_connection()
    try:
        cursor = conn.cursor()
        row = cursor.execute(
            "SELECT pass_hash FROM operators WHERE name = ?", (name,)
        ).fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail="代号不存在，请先登录一次")

        stored = row["pass_hash"]
        if stored:
            if not req.current_password or not verify_password(req.current_password, stored):
                raise HTTPException(status_code=403, detail="当前口令不正确")

        new = (req.new_password or "").strip()
        if new:
            if len(new) < 4:
                raise HTTPException(status_code=400, detail="口令至少 4 位")
            cursor.execute("UPDATE operators SET pass_hash = ? WHERE name = ?",
                           (hash_password(new), name))
            conn.commit()
            return {"status": "ok", "protected": True}
        cursor.execute("UPDATE operators SET pass_hash = NULL WHERE name = ?", (name,))
        conn.commit()
        return {"status": "ok", "protected": False}
    finally:
        conn.close()
