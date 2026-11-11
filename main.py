#!/usr/bin/env python3
# -*- coding: utf-8 -*-
#
# Project: PaperLab - AI Automated OSCP Lab Generator
# Author: tw1t
# Copyright 2026 tw1t
# SPDX-License-Identifier: Apache-2.0
import os
import json
import sqlite3
import asyncio
import base64
import hashlib
import hmac
import functools
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Optional
from urllib.parse import quote

from fastapi import FastAPI, HTTPException, UploadFile, File, Query, Request, Header
from fastapi.responses import FileResponse, StreamingResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from openai import OpenAI

from paperlab import grading
from paperlab import learning_path
from paperlab.learning_path import build_learning_path
from fastapi.middleware.cors import CORSMiddleware

# ==========================================
# 1. 核心配置 (从 lab_generator 统一加载)
# ==========================================
from lab_generator import (
    load_config,
    ensure_db,
    parse_markdown_text_to_machines,
    get_all_used_machine_names,
    generate_single_variant,
    generation_stats_summary,
    clean_and_parse_json,
    _json_mode_arg,
    _disable_json_mode,
    _response_format_rejected,
    DB_FILE,
    ALLOWED_DOMAINS,
    ALLOWED_DIFFICULTIES,
)

# 配置与 OpenAI 客户端在 llm.py（生成/判卷/健康检查都要用）。
from paperlab.llm import AI_MODEL, CFG as _cfg, client  # noqa: E402

# 接口文档默认关闭（自建工具无需暴露清单）；调试时设 PAPERLAB_ENABLE_DOCS=1。
_ENABLE_DOCS = os.environ.get("PAPERLAB_ENABLE_DOCS", "0") == "1"

app = FastAPI(
    title="PaperLab - Pro Examiner Edition",
    docs_url="/docs" if _ENABLE_DOCS else None,
    redoc_url="/redoc" if _ENABLE_DOCS else None,
    openapi_url="/openapi.json" if _ENABLE_DOCS else None,
)

# CORS 默认只放行本机页面。
# 不要改成 "*"：本服务本身无鉴权，通配来源等于让浏览器上任意网页都能
# 读取数据、或触发会真实计费的上传裂变。
# 局域网共享时用环境变量显式声明，例如：
#   PAPERLAB_ORIGINS=http://192.168.1.10:8000,http://127.0.0.1:8000
_DEFAULT_ORIGINS = "http://127.0.0.1:8000,http://localhost:8000"
_ALLOW_ORIGINS = [o.strip() for o in
                  os.environ.get("PAPERLAB_ORIGINS", _DEFAULT_ORIGINS).split(",")
                  if o.strip()]

from paperlab.routers.analytics import router as analytics_router  # noqa: E402
from paperlab.routers.labs import router as labs_router  # noqa: E402
from paperlab.routers.bookmarks import router as bookmarks_router  # noqa: E402
from paperlab.routers.review import router as review_router  # noqa: E402
from paperlab.routers.stats import router as stats_router  # noqa: E402
from paperlab.routers.history import router as history_router  # noqa: E402
from paperlab.routers.system import router as system_router  # noqa: E402
from paperlab.routers.grading import router as grading_router  # noqa: E402
from paperlab.routers.upload import router as upload_router  # noqa: E402
from paperlab.stats_service import compute_user_stats  # noqa: E402

app.add_middleware(
    CORSMiddleware,
    allow_origins=_ALLOW_ORIGINS,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(analytics_router)
app.include_router(labs_router)
app.include_router(bookmarks_router)
app.include_router(review_router)
app.include_router(stats_router)
app.include_router(history_router)
app.include_router(system_router)
app.include_router(grading_router)
app.include_router(upload_router)


_STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")
if os.path.isdir(_STATIC_DIR):
    app.mount("/static", StaticFiles(directory=_STATIC_DIR), name="static")


# ==========================================
# 2. 数据库初始化（启动时一次性建表 + 迁移 + 索引）
# ==========================================
def init_db():
    ensure_db(DB_FILE)


init_db()


def _print_banner():
    conn = sqlite3.connect(DB_FILE)
    try:
        lab_count = conn.execute("SELECT COUNT(*) FROM labs").fetchone()[0]
    finally:
        conn.close()
    print("=" * 50)
    print("  OSCP Paper Lab — Pro Examiner Edition")
    print("=" * 50)
    print(f"  模型  : {AI_MODEL}")
    print(f"  端点  : {_cfg['base_url']}")
    print(f"  靶机库: {lab_count} 台")
    print("  地址  : http://127.0.0.1:8000")
    if not _ENABLE_DOCS:
        print("  文档  : 已关闭（设 PAPERLAB_ENABLE_DOCS=1 可开启）")
    print("=" * 50)


_print_banner()


# 数据库连接与口令校验搬到 deps.py（路由拆分后共享，避免循环导入）。
from paperlab.deps import (  # noqa: E402
    get_db_connection,
    guard_operator,
    lookup_pass_hash as _lookup_pass_hash,
    safe_json as _safe_json,
)


# ==========================================
# 3. 可选口令隔离
#    - 未设置口令的代号：零门槛（保持向后兼容）
#    - 设置了口令的代号：后续请求需带 X-Operator-Pass
# ==========================================
# 口令哈希与校验搬到 auth.py（纯函数，可单独测试）。
# 这里保留同名别名，调用处与既有测试都不必改动。
from paperlab.auth import (  # noqa: E402
    AuthRequest,
    hash_password as _hash_password,
    legacy_hash_password as _legacy_hash_password,
    needs_rehash as _needs_rehash,
    verify_password as _verify_password,
)


if __name__ == "__main__":
    import uvicorn

    # 直接 `python main.py` 即可启动，无需了解 uvicorn。
    # 需要 --reload 或局域网 --host 0.0.0.0 时，改用 uvicorn main:app 自行传参。
    uvicorn.run(app, host="127.0.0.1", port=8000)
