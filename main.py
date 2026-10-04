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
import hashlib
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
    clean_and_parse_json,
    _json_mode_arg,
    _disable_json_mode,
    _response_format_rejected,
    DB_FILE,
    ALLOWED_DOMAINS,
    ALLOWED_DIFFICULTIES,
)

_cfg = load_config()

client = OpenAI(
    api_key=_cfg["api_key"],
    base_url=_cfg["base_url"],
    timeout=120.0
)
AI_MODEL = _cfg["model"]

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

app.add_middleware(
    CORSMiddleware,
    allow_origins=_ALLOW_ORIGINS,
    allow_methods=["*"],
    allow_headers=["*"],
)

MAX_DERIVE_COUNT = 10
DEFAULT_WORKERS = 3
MAX_WORKERS = 10

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


def get_db_connection():
    conn = sqlite3.connect(DB_FILE, timeout=30)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.row_factory = sqlite3.Row
    return conn


# ==========================================
# 3. 可选口令隔离
#    - 未设置口令的代号：零门槛（保持向后兼容）
#    - 设置了口令的代号：后续请求需带 X-Operator-Pass
# ==========================================
def _hash_password(raw: str) -> str:
    return hashlib.sha256(("paperlab::" + raw).encode("utf-8")).hexdigest()


def _lookup_pass_hash(name: str) -> Optional[str]:
    conn = get_db_connection()
    try:
        row = conn.execute(
            "SELECT pass_hash FROM operators WHERE name = ?", (name,)
        ).fetchone()
        return row["pass_hash"] if row else None
    finally:
        conn.close()


def _safe_json(raw, default):
    """历史数据里可能存在非法 JSON，解析失败时降级为默认值而不是抛 500。"""
    try:
        return json.loads(raw) if raw else default
    except (json.JSONDecodeError, TypeError):
        return default


def guard_operator(username: str, provided_pass: Optional[str]) -> str:
    """
    校验某代号是否有权操作。
    未注册 / 未设口令 -> 放行（向后兼容）；设了口令 -> 必须匹配。
    """
    stored = _lookup_pass_hash(username)
    if not stored:
        return username
    if not provided_pass or _hash_password(provided_pass) != stored:
        raise HTTPException(status_code=403, detail="该代号已启用口令保护，凭据不正确")
    return username


class AuthRequest(BaseModel):
    name: str
    password: Optional[str] = None


@app.post("/api/auth")
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
            new_hash = _hash_password(req.password) if req.password else None
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
                    (_hash_password(req.password), name)
                )
                conn.commit()
                return {"status": "ok", "mode": "protected", "protected": True}
            return {"status": "ok", "mode": "open", "protected": False}
        if not req.password:
            raise HTTPException(status_code=401, detail="该代号已启用口令保护，请输入口令")
        if _hash_password(req.password) != stored:
            raise HTTPException(status_code=401, detail="口令不正确")
        return {"status": "ok", "mode": "verified", "protected": True}
    finally:
        conn.close()


class StudentSubmission(BaseModel):
    lab_id: str
    username: str
    answers: dict


# ==========================================
# 4. 业务路由 API
# ==========================================

@app.get("/")
async def serve_frontend():
    if os.path.exists("index.html"):
        return FileResponse("index.html")
    return {"error": "index.html not found"}


@app.get("/healthz")
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


@app.get("/api/list_labs")
async def list_labs(
    os_filter: Optional[str] = Query(None, alias="os"),
    domain: Optional[str] = Query(None),
    difficulty: Optional[str] = Query(None),
):
    try:
        conn = get_db_connection()
        try:
            conditions = []
            params = []
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


@app.get("/api/filters")
async def get_filters():
    """返回规范的筛选项，供前端下拉使用（避免被脏数据污染出十几个碎片选项）。"""
    return {"domains": ALLOWED_DOMAINS, "difficulties": ALLOWED_DIFFICULTIES}


@app.get("/api/done_labs")
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


@app.get("/api/get_lab/{lab_id}")
async def get_lab_detail(lab_id: str):
    conn = get_db_connection()
    try:
        row = conn.execute("SELECT * FROM labs WHERE id = ?", (lab_id,)).fetchone()
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


def _grade_once(system_prompt, user_prompt):
    """
    调用阅卷模型并解析结果。
    与生成侧同样采用「只重试计费调用本身」的策略；
    但这里每个变种的阅卷是独立的一题，重试不会连带任何持久化操作。
    """
    last_error = "未知错误"
    raw = ""
    attempts = 0
    while attempts < 3:
        kwargs = {
            "model": AI_MODEL,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt}
            ],
            "temperature": 0.1,  # 低温保证评分的一致性
            "max_tokens": 3000,
        }
        kwargs.update(_json_mode_arg())
        try:
            response = client.chat.completions.create(**kwargs)
            raw = response.choices[0].message.content
            return clean_and_parse_json(raw), None
        except json.JSONDecodeError:
            attempts += 1
            last_error = "AI 返回了无效的成绩单格式"
        except Exception as e:
            if _response_format_rejected(e) and _disable_json_mode():
                continue                  # 刚降级，用不带 response_format 的参数重试
            attempts += 1
            last_error = f"AI 判卷通信故障: {str(e)}"
    return None, last_error


@app.post("/api/evaluate")
async def evaluate_submission(
    submission: StudentSubmission,
    x_operator_pass: Optional[str] = Header(None, alias="X-Operator-Pass"),
):
    """判卷引擎：全知全能的毒舌考官 + 异常防线"""
    guard_operator(submission.username, x_operator_pass)

    conn = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor()

        cursor.execute(
            "SELECT context, questions, focus_points FROM labs WHERE id = ?",
            (submission.lab_id,)
        )
        lab_data = cursor.fetchone()

        if not lab_data:
            raise HTTPException(status_code=404, detail="靶机未找到")

        student_writeup = submission.answers.get("student_writeup", "未提供内容")

        system_prompt = """
        # Role
        你是一位极度挑剔、技术深厚的 OSCP 资深考官。
        
        # Task
        你将获得该靶机的 [终端日志(情报)]、[考核问题]、[考官底牌] 以及学生的 [推演作答]。
        请仔细比对学生是否从【终端日志】中精准提取了线索，并推理出了符合【考官底牌】的攻击链。
        如果学生漏掉了核心技术（如具体的漏洞名、CVE、工具命令、敏感文件名或绝对路径），必须严厉扣分！
        
        # Output Format (Strict JSON)
        必须严格输出 JSON 格式。
        {
            "evaluation_report": {
                "executive_summary": "总体评价（语气要硬核、专业、极其毒舌，一针见血指出致命失误）",
                "strengths": ["亮点"],
                "areas_for_improvement": ["技术短板"],
                "recommended_focus_domains": ["建议学习领域"]
            },
            "question_feedback": [
                {
                    "question_id": 1,
                    "score": 8,
                    "feedback": "具体的技术性评价。结合终端日志指出为何扣分，语气要严厉。",
                    "missed_key_insights": ["漏掉的核心名词，如：'SeImpersonatePrivilege', '未发现 .htpasswd 文件'"] 
                }
            ]
        }
        """

        user_prompt = f"""
        # [The Battlefield (Terminal Logs - 学生看到的情报)]
        {lab_data['context']}
        
        # [The Questions (给学生的任务)]
        {lab_data['questions']}

        # [Hidden Rubric (考官底牌/预期路径)]
        {lab_data['focus_points']}
        
        # [Student's Technical Response (学生推演作答)]
        {student_writeup}
        """

        # 放到线程池执行，避免阻塞事件循环
        loop = asyncio.get_running_loop()
        ai_report, grade_error = await loop.run_in_executor(
            None, functools.partial(_grade_once, system_prompt, user_prompt)
        )
        if ai_report is None:
            raise HTTPException(status_code=502, detail=grade_error or "AI 判卷失败")

        # 落库时同步算出本次平均分，供排行榜直接 SQL 聚合，无需事后解析 JSON
        _fb = ai_report.get("question_feedback", []) if isinstance(ai_report, dict) else []
        _scores = [q["score"] for q in _fb
                   if isinstance(q, dict) and isinstance(q.get("score"), (int, float))]
        submission_avg = round(sum(_scores) / len(_scores), 1) if _scores else 0.0

        cursor.execute('''
            INSERT INTO submissions (lab_id, operator_name, student_writeup, report, avg_score)
            VALUES (?, ?, ?, ?, ?)
        ''', (submission.lab_id, submission.username, student_writeup,
              json.dumps(ai_report, ensure_ascii=False), submission_avg))
        conn.commit()

        return ai_report

    except HTTPException:
        raise
    except Exception as e:
        print(f"判卷异常: {e}")
        raise HTTPException(status_code=500, detail=f"AI 判卷通信故障: {str(e)}")
    finally:
        if conn:
            conn.close()



class HintRequest(BaseModel):
    lab_id: str
    question_idx: int
    username: Optional[str] = None


HINT_SYSTEM_PROMPT = """你是 OSCP 考官助手。学生请求提示。
你的任务：从考点中，为指定题目提炼一个【方向性提示】。
规则：
1. 只给方向，绝对不给具体漏洞名/CVE/命令/路径。
2. 提示必须是中文，1-2 句话，简洁有力。
3. 可以引导学生思考"应该关注情报的哪一部分"，但不能直接说"漏洞是X"。
4. 只输出纯文本提示内容，无需 JSON 包裹。"""


def _hint_sync(question_text, question_focus, focus_points):
    """同步生成方向提示；由线程池调用，避免阻塞事件循环。"""
    user_prompt = (
        f"题目：{question_text}\n"
        f"考点（仅供你参考，绝对不能泄露）：{question_focus}\n"
        f"全局考点：{focus_points}\n\n"
        "请输出一条对学生的方向性提示："
    )
    last_error = "未知错误"
    for attempt in range(3):
        try:
            resp = client.chat.completions.create(
                model=AI_MODEL,
                messages=[
                    {"role": "system", "content": HINT_SYSTEM_PROMPT},
                    {"role": "user", "content": user_prompt},
                ],
                temperature=0.4,
                max_tokens=200,
            )
            return (resp.choices[0].message.content or "").strip(), None
        except Exception as e:
            last_error = str(e)
            if attempt < 2:
                time.sleep(2 ** attempt)
    return None, last_error


@app.post("/api/hint")
async def get_hint(req: HintRequest):
    """为指定题目生成方向提示（只给方向，不泄露答案）。"""
    if req.username:
        guard_operator(req.username, None)

    conn = get_db_connection()
    try:
        row = conn.execute(
            "SELECT questions, focus_points FROM labs WHERE id = ?", (req.lab_id,)
        ).fetchone()
    finally:
        conn.close()

    if not row:
        raise HTTPException(status_code=404, detail="靶机未找到")

    questions = _safe_json(row["questions"], [])
    if req.question_idx < 0 or req.question_idx >= len(questions):
        raise HTTPException(status_code=400, detail="题目编号超出范围")

    q = questions[req.question_idx]
    loop = asyncio.get_running_loop()
    hint, err = await loop.run_in_executor(
        None,
        functools.partial(_hint_sync, q.get("text", ""), q.get("focus", ""), row["focus_points"]),
    )
    if hint is None:
        raise HTTPException(status_code=503, detail=f"提示生成失败：{err}")
    return {"hint": hint}


@app.get("/api/history")
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
            "SELECT * FROM submissions WHERE operator_name = ? ORDER BY timestamp DESC LIMIT ? OFFSET ?",
            (username, page_size, offset)
        )
        rows = cursor.fetchall()

        history_list = []
        for row in rows:
            try:
                report = json.loads(row["report"])
            except (json.JSONDecodeError, TypeError):
                continue  # 跳过损坏的历史记录
            scores = [q["score"] for q in report.get("question_feedback", [])]
            avg = round(sum(scores) / len(scores), 1) if scores else 0
            summary_raw = report.get("evaluation_report", {}).get("executive_summary", "")
            history_list.append({
                "id": row["id"],
                "lab_id": row["lab_id"],
                "timestamp": row["timestamp"],
                "avg_score": avg,
                "summary": summary_raw[:50] + "..." if len(summary_raw) > 50 else summary_raw,
                "report": report
            })
        return {"items": history_list, "total": total, "page": page, "page_size": page_size}
    finally:
        conn.close()


# ==========================================
# 5. 错题本 API
# ==========================================

class BookmarkItem(BaseModel):
    username: str
    lab_id: str
    question_text: str
    question_focus: str
    missed_insights: list
    feedback: str
    score: int


@app.post("/api/bookmarks")
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
            INSERT INTO bookmarks (operator_name, lab_id, question_text, question_focus, missed_insights, feedback, score)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        ''', (
            item.username, item.lab_id, item.question_text, item.question_focus,
            json.dumps(item.missed_insights, ensure_ascii=False), item.feedback, item.score
        ))
        conn.commit()
        return {"status": "ok", "id": cursor.lastrowid}
    finally:
        conn.close()


@app.get("/api/bookmarks")
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
        result = []
        for row in rows:
            try:
                missed = json.loads(row["missed_insights"])
            except (json.JSONDecodeError, TypeError):
                missed = []
            result.append({
                "id": row["id"],
                "lab_id": row["lab_id"],
                "question_text": row["question_text"],
                "question_focus": row["question_focus"],
                "missed_insights": missed,
                "feedback": row["feedback"],
                "score": row["score"],
                "timestamp": row["timestamp"],
            })
        return result
    finally:
        conn.close()


@app.delete("/api/bookmarks/{bookmark_id}")
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


@app.get("/api/bookmarks/export")
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
            f"> 导出时间：{__import__('datetime').datetime.now().strftime('%Y-%m-%d %H:%M')}",
            f"> 共 {len(rows)} 条错题记录",
            "",
        ]
        for i, row in enumerate(rows, 1):
            try:
                missed = json.loads(row["missed_insights"])
            except (json.JSONDecodeError, TypeError):
                missed = []
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
        # 依 RFC 5987 用 filename*=UTF-8'' 提供，并保留 ASCII 兜底文件名。
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


# ==========================================
# 6. MD 文件上传 → 实时裂变生成（SSE）
# ==========================================
MAX_UPLOAD_SIZE = 5 * 1024 * 1024  # 5 MB


@app.post("/api/upload_and_build")
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
SM2_MAX_INTERVAL = 180


def _sm2_update(easiness: float, interval: int, repetitions: int, score: int):
    """
    SM-2 算法核心计算。
    score: 0-5
    返回: (new_easiness, new_interval, new_repetitions)
    """
    if score < 3:
        # 答错，重置
        repetitions = 0
        interval = 1
    else:
        if repetitions == 0:
            interval = 1
        elif repetitions == 1:
            interval = 6
        else:
            interval = round(interval * easiness)
        repetitions += 1

    interval = max(1, min(int(interval), SM2_MAX_INTERVAL))
    easiness = max(1.3, easiness + 0.1 - (5 - score) * (0.08 + (5 - score) * 0.02))
    return round(easiness, 2), interval, repetitions


class SM2ReviewItem(BaseModel):
    username: str
    lab_id: str
    question_idx: int
    question_text: str
    score_10: int  # 前端传 0-10 的原始分，后端折算为 SM-2 的 0-5


@app.post("/api/sm2/review")
async def sm2_review(
    item: SM2ReviewItem,
    x_operator_pass: Optional[str] = Header(None, alias="X-Operator-Pass"),
):
    """提交一道题的复盘评分，更新 SM-2 调度"""
    guard_operator(item.username, x_operator_pass)
    score_10 = max(0, min(10, int(item.score_10)))
    score_5 = round(score_10 / 2)  # 10分制 → 5分制
    conn = get_db_connection()
    try:
        cursor = conn.cursor()
        row = cursor.execute(
            "SELECT easiness, interval, repetitions FROM sm2_schedule "
            "WHERE operator_name=? AND lab_id=? AND question_idx=?",
            (item.username, item.lab_id, item.question_idx)
        ).fetchone()
        easiness = row["easiness"] if row else 2.5
        interval = row["interval"] if row else 1
        repetitions = row["repetitions"] if row else 0

        new_e, new_i, new_r = _sm2_update(easiness, interval, repetitions, score_5)

        # date('now') 是 UTC，会让北京时间 00:00–08:00 的「今日复习」取不到当天卡片，故用 localtime。
        cursor.execute('''
            INSERT INTO sm2_schedule (operator_name, lab_id, question_idx, question_text, easiness, interval, repetitions, next_review, last_score)
            VALUES (?, ?, ?, ?, ?, ?, ?, date('now', 'localtime', ? || ' days'), ?)
            ON CONFLICT(operator_name, lab_id, question_idx) DO UPDATE SET
                easiness=excluded.easiness,
                interval=excluded.interval,
                repetitions=excluded.repetitions,
                next_review=excluded.next_review,
                last_score=excluded.last_score
        ''', (
            item.username, item.lab_id, item.question_idx, item.question_text,
            new_e, new_i, new_r, str(new_i), score_10
        ))
        conn.commit()
        return {"status": "ok", "next_review_in_days": new_i,
                "easiness": new_e, "repetitions": new_r}
    finally:
        conn.close()


@app.get("/api/sm2/today")
async def sm2_today(
    username: str,
    x_operator_pass: Optional[str] = Header(None, alias="X-Operator-Pass"),
):
    """返回今日需要复盘的题目列表（next_review <= today），含对应考点 focus"""
    guard_operator(username, x_operator_pass)
    conn = get_db_connection()
    try:
        rows = conn.execute('''
            SELECT s.id, s.lab_id, s.question_idx, s.question_text,
                   s.easiness, s.interval, s.repetitions, s.next_review, s.last_score,
                   l.os, l.difficulty, l.domain, l.questions AS questions_json
            FROM sm2_schedule s
            LEFT JOIN labs l ON s.lab_id = l.id
            WHERE s.operator_name = ? AND s.next_review <= date('now', 'localtime')
            ORDER BY s.next_review ASC
        ''', (username,)).fetchall()
        result = []
        for row in rows:
            card = dict(row)
            try:
                questions = json.loads(card.pop("questions_json") or "[]")
                idx = card["question_idx"]
                card["question_focus"] = questions[idx]["focus"] if idx < len(questions) else ""
            except Exception:
                card["question_focus"] = ""
            result.append(card)
        return result
    finally:
        conn.close()


@app.get("/api/sm2/stats")
async def sm2_stats(
    username: str,
    x_operator_pass: Optional[str] = Header(None, alias="X-Operator-Pass"),
):
    """返回用户 SM-2 整体进度统计"""
    guard_operator(username, x_operator_pass)
    conn = get_db_connection()
    try:
        cursor = conn.cursor()
        total = cursor.execute(
            "SELECT COUNT(*) AS total FROM sm2_schedule WHERE operator_name=?", (username,)
        ).fetchone()["total"]
        due = cursor.execute(
            "SELECT COUNT(*) AS due FROM sm2_schedule "
            "WHERE operator_name=? AND next_review <= date('now', 'localtime')", (username,)
        ).fetchone()["due"]
        avg_row = cursor.execute(
            "SELECT AVG(last_score) AS avg_score FROM sm2_schedule WHERE operator_name=?",
            (username,)
        ).fetchone()
        avg_score = round(avg_row["avg_score"] or 0, 1)
        return {"total_cards": total, "due_today": due, "avg_last_score": avg_score}
    finally:
        conn.close()


# ==========================================
# 8. 个人统计面板 API + 排行榜
# ==========================================

@app.get("/api/leaderboard")
async def get_leaderboard(limit: int = Query(20, ge=1, le=100)):
    """
    全平台排行榜：按平均分降序，平均分相同则按总次数降序。
    直接以 SQL 聚合 avg_score 列，避免把全部战报读进内存解析 JSON。
    """
    conn = get_db_connection()
    try:
        rows = conn.execute(
            """
            SELECT operator_name,
                   ROUND(AVG(avg_score), 1) AS avg_score,
                   COUNT(*) AS total_submissions
            FROM submissions
            WHERE avg_score > 0
            GROUP BY operator_name
            ORDER BY avg_score DESC, total_submissions DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()

        board = []
        for i, row in enumerate(rows, 1):
            board.append({
                "rank": i,
                "operator_name": row["operator_name"],
                "avg_score": row["avg_score"],
                "total_submissions": row["total_submissions"],
            })
        return board

    finally:
        conn.close()


@app.get("/api/streak")
async def get_streak(username: str, x_operator_pass: Optional[str] = Header(None, alias="X-Operator-Pass")):
    """返回用户连续打卡天数（当前连续 / 历史最长）。"""
    guard_operator(username, x_operator_pass)

    conn = get_db_connection()
    try:
        rows = conn.execute(
            "SELECT DISTINCT date(timestamp, 'localtime') AS day FROM submissions "
            "WHERE operator_name = ? ORDER BY day DESC",
            (username,),
        ).fetchall()
    finally:
        conn.close()

    days = [r["day"] for r in rows if r["day"]]
    if not days:
        return {"streak": 0, "longest": 0, "today": False}

    from datetime import date, timedelta
    day_set = set(days)
    today = date.today()

    # 当天或昨天都算作连续起点（当天还没做题时，昨天做了也算连续）
    check = today if today.isoformat() in day_set else today - timedelta(days=1)
    streak = 0
    while check.isoformat() in day_set:
        streak += 1
        check -= timedelta(days=1)

    longest = cur = 0
    prev = None
    for d in sorted(day_set):
        cur_d = date.fromisoformat(d)
        cur = cur + 1 if (prev and (cur_d - prev).days == 1) else 1
        longest = max(longest, cur)
        prev = cur_d

    return {"streak": streak, "longest": longest, "today": today.isoformat() in day_set}


@app.get("/api/stats")
async def get_stats(
    username: str,
    x_operator_pass: Optional[str] = Header(None, alias="X-Operator-Pass"),
):
    """返回用户个人统计数据：各 Domain 平均分、Tag 维度分析、总体趋势"""
    guard_operator(username, x_operator_pass)
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

        domain_data = {}
        tag_data = {}
        trend = []
        all_avgs = []

        for row in rows:
            try:
                report = json.loads(row["report"])
            except (json.JSONDecodeError, TypeError):
                continue
            scores = [q["score"] for q in report.get("question_feedback", [])
                      if isinstance(q.get("score"), (int, float))]
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


# ==========================================
# 9. 入口
# ==========================================
if __name__ == "__main__":
    import uvicorn

    # 直接 `python main.py` 即可启动，无需了解 uvicorn。
    # 需要 --reload 或局域网 --host 0.0.0.0 时，改用 uvicorn main:app 自行传参。
    uvicorn.run(app, host="127.0.0.1", port=8000)
