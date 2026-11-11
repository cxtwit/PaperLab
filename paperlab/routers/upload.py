#!/usr/bin/env python3
# -*- coding: utf-8 -*-
#
# Project: PaperLab - AI Automated OSCP Lab Generator
# Author: tw1t
# Copyright 2026 tw1t
# SPDX-License-Identifier: Apache-2.0
"""
上传 → 实时裂变生成（SSE）。

这是全项目唯一一条有状态机的路由：前端用 EventSource 建立长连接，
后端把每个变种的生成结果边完成边推送（`asyncio.as_completed`），
所以不能用普通的 return，必须 StreamingResponse。

三个上限是**保护自己**的，不是给别人设的：

    MAX_UPLOAD_SIZE      单个 md 文件最大 5MB
    MAX_UPLOAD_MACHINES  单次最多解析多少台母体（默认 20）
    MAX_TOTAL_VARIANTS   单次最多生成多少变体（母体数 × 变种数，默认 100）

任务量是乘起来的：一个 5MB 的 md 可能解析出上百台母体，一次点击就是
几百上千次计费调用。前端传的 derive_count / workers 也各自有上界。
"""

import asyncio
import functools
import json
from concurrent.futures import ThreadPoolExecutor
from typing import Optional

from fastapi import APIRouter, File, HTTPException, Query, Request, UploadFile
from fastapi.responses import StreamingResponse

from lab_generator import (
    DB_FILE,
    generate_single_variant,
    get_all_used_machine_names,
    parse_markdown_text_to_machines,
)
from ..llm import AI_MODEL, client

MAX_DERIVE_COUNT = 10
DEFAULT_WORKERS = 3
MAX_WORKERS = 10

router = APIRouter()


MAX_UPLOAD_SIZE = 5 * 1024 * 1024  # 5 MB
# 单次上传的任务量是 母体数 × 变种数。以前只限制了"线程数"和"单个母体的变种数"，
# 却没限制文件里能解析出多少台母体 —— 一个 5MB 的 md 可能解析出上百台，
# 一次点击就是几百上千次计费调用。这两个上限是**保护自己**，不是给别人设的。
MAX_UPLOAD_MACHINES = 20    # 单次上传最多解析多少个 ## 母体
MAX_TOTAL_VARIANTS = 100    # 单次上传最多生成多少个变体（母体数 × derive_count）

@router.post("/api/upload_and_build")
async def upload_and_build(
    request: Request,
    file: UploadFile = File(...),
    derive_count: int = Query(3, ge=1, le=MAX_DERIVE_COUNT),
    enable_quality_check: bool = True,
    workers: int = Query(DEFAULT_WORKERS, ge=1, le=MAX_WORKERS),
):
    """
    接收上传的 .md 文件，解析其中所有靶机母体，
    以 SSE 流（text/event-stream）实时推送每台靶机的生成进度。
    前端通过 EventSource 接收。

    derive_count 与 workers 是直接乘算 LLM 调用次数的乘数，必须带上界。
    """
    if not file.filename.endswith(".md"):
        raise HTTPException(status_code=400, detail="只支持 .md 格式文件")

    content_bytes = await file.read(MAX_UPLOAD_SIZE + 1)
    if len(content_bytes) > MAX_UPLOAD_SIZE:
        raise HTTPException(
            status_code=413,
            detail=f"文件超过最大限制 {MAX_UPLOAD_SIZE // 1024 // 1024} MB，请拆分后上传"
        )
    try:
        md_text = content_bytes.decode("utf-8")
    except UnicodeDecodeError:
        md_text = content_bytes.decode("gbk", errors="replace")

    machines = parse_markdown_text_to_machines(md_text)
    if not machines:
        raise HTTPException(status_code=400, detail="未在文件中找到任何 ## 标题分隔的靶机母体")

    # 任务量 = 母体数 × 变种数。两个都要有上界，否则一次上传就可能烧掉大量额度。
    if len(machines) > MAX_UPLOAD_MACHINES:
        raise HTTPException(
            status_code=400,
            detail=(f"文件里解析出 {len(machines)} 台母体，超过单次上限 {MAX_UPLOAD_MACHINES} 台。"
                    f"请把 md 拆成多份，或减少其中的 ## 标题数量")
        )
    planned = len(machines) * derive_count
    if planned > MAX_TOTAL_VARIANTS:
        raise HTTPException(
            status_code=400,
            detail=(f"本次将生成 {planned} 个变体（{len(machines)} 台母体 × {derive_count} 变种），"
                    f"超过单次上限 {MAX_TOTAL_VARIANTS} 个。请减少变种数或拆分文件")
        )

    global_used_names = get_all_used_machine_names(DB_FILE)
    jobs = [(oid, wp_text, vi)
            for oid, wp_text in machines.items()
            for vi in range(derive_count)]

    async def event_stream():
        total_machines = len(machines)
        total_variants = len(jobs)
        done = 0

        yield f"data: {json.dumps({'type': 'start', 'total': total_variants, 'machines': total_machines, 'workers': workers}, ensure_ascii=False)}\n\n"

        for oid in machines:
            yield f"data: {json.dumps({'type': 'machine_start', 'machine': oid, 'derive_count': derive_count}, ensure_ascii=False)}\n\n"

        loop = asyncio.get_running_loop()
        pool = ThreadPoolExecutor(max_workers=workers)
        try:
            futures = []
            for oid, wp_text, vi in jobs:
                fut = loop.run_in_executor(
                    pool,
                    functools.partial(
                        generate_single_variant,
                        client, AI_MODEL, wp_text, oid, vi,
                        global_used_names, DB_FILE, enable_quality_check
                    )
                )
                futures.append((fut, oid, vi))

            # 谁先完成先推送，保持 SSE 实时性
            for coro in asyncio.as_completed([f for f, _, _ in futures]):
                success, new_name, lab_data, error = await coro
                done += 1
                progress = round(done / total_variants * 100)

                if success:
                    payload = {
                        'type': 'variant_ok', 'machine': new_name,
                        'domain': (lab_data or {}).get('domain'),
                        'difficulty': (lab_data or {}).get('difficulty'),
                        'done': done, 'total': total_variants, 'progress': progress,
                    }
                else:
                    payload = {
                        'type': 'variant_fail', 'error': error,
                        'done': done, 'total': total_variants, 'progress': progress,
                    }
                yield f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"
                await asyncio.sleep(0)
        finally:
            pool.shutdown(wait=False)

        yield f"data: {json.dumps({'type': 'done', 'total': total_variants, 'done': done}, ensure_ascii=False)}\n\n"

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"}
    )


# ==========================================
# 7. SM-2 间隔重复复盘 API
# ==========================================

# 复习间隔上限（天）：SM-2 原版会把间隔推到数年，对备考周期无意义。


# ==========================================
# 9. 入口
# ==========================================
