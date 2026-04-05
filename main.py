#!/usr/bin/env python3
# -*- coding: utf-8 -*-
#
# Project: PaperLab - AI Automated OSCP Lab Generator
# Author: tw1t
# 
# This project is licensed under the GNU GPLv3 License (或者 CC BY-NC 4.0).
# COMMERCIAL USE IS STRICTLY PROHIBITED WITHOUT EXPLICIT PERMISSION.
# 严禁将本项目及其 Prompt 逻辑用于任何形式的商业盈利目的！
import os
import json
import sqlite3
import re
import asyncio
from typing import Optional
from fastapi import FastAPI, HTTPException, UploadFile, File, Query, Request
from fastapi.responses import FileResponse, StreamingResponse
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
    MUTATION_ANGLES,
)

_cfg = load_config()

client = OpenAI(
    api_key=_cfg["api_key"],
    base_url=_cfg["base_url"],
    timeout=120.0
)
AI_MODEL = _cfg["model"]

app = FastAPI(title="PaperLab - Pro Examiner Edition", docs_url=None, redoc_url=None)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

DB_FILE = "paperlab.db"

# ==========================================
# 2. 数据库初始化（启动时一次性建表）
# ==========================================
def init_db():
    ensure_db(DB_FILE)

init_db()

# 启动 Banner
def _print_banner():
    conn = sqlite3.connect(DB_FILE)
    lab_count = conn.execute("SELECT COUNT(*) FROM labs").fetchone()[0]
    conn.close()
    print("=" * 50)
    print("  OSCP Paper Lab — Pro Examiner Edition")
    print("=" * 50)
    print(f"  模型  : {AI_MODEL}")
    print(f"  端点  : {_cfg['base_url']}")
    print(f"  靶机库: {lab_count} 台")
    print(f"  地址  : http://127.0.0.1:8000")
    print("=" * 50)

_print_banner()
def get_db_connection():
    conn = sqlite3.connect(DB_FILE)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.row_factory = sqlite3.Row
    return conn

# 💡 整合：保留了 username 字段，支持前端的多用户隔离
class StudentSubmission(BaseModel):
    lab_id: str
    username: str 
    answers: dict

# ==========================================
# 3. 业务路由 API
# ==========================================

@app.get("/")
async def serve_frontend():
    if os.path.exists("index.html"):
        return FileResponse("index.html")
    return {"error": "index.html not found"}

@app.get("/api/list_labs")
async def list_labs(
    os_filter: Optional[str] = Query(None, alias="os"),
    domain: Optional[str] = Query(None),
    difficulty: Optional[str] = Query(None),
):
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        # 动态构建过滤条件
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
        cursor.execute(f"SELECT id, os, difficulty, domain FROM labs {where_clause}", params)
        rows = cursor.fetchall()
        conn.close()
        return [dict(row) for row in rows]
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Database Error: {str(e)}")

@app.get("/api/done_labs")
async def get_done_labs(username: str):
    """返回该用户做过的所有靶机 id 列表（用于前端完整排除已做）"""
    conn = get_db_connection()
    try:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT DISTINCT lab_id FROM submissions WHERE operator_name = ?",
            (username,)
        )
        rows = cursor.fetchall()
        return [row["lab_id"] for row in rows]
    finally:
        conn.close()


@app.get("/api/get_lab/{lab_id}")
async def get_lab_detail(lab_id: str):
    """适配 Pro 版 Schema，包含 OS、难度、领域和标签"""
    conn = get_db_connection()
    try:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM labs WHERE id = ?", (lab_id,))
        row = cursor.fetchone()
    finally:
        conn.close()

    if not row:
        raise HTTPException(status_code=404, detail="靶机未找到")
    
    return {
        "id": row["id"],
        "os": row["os"],
        "difficulty": row["difficulty"],
        "domain": row["domain"],
        "tags": json.loads(row["tags"]),
        "context": row["context"],
        "questions": json.loads(row["questions"]),
    }

@app.post("/api/evaluate")
async def evaluate_submission(submission: StudentSubmission):
    """Pro 级判卷引擎：全知全能的毒舌考官 + 异常防线"""
    conn = None
    raw_json_str = ""
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        
        # 💡 改进 1：不仅拿考官标准，还把“案发现场(context)”和“考试题目(questions)”全拿出来喂给考官
        cursor.execute("SELECT context, questions, focus_points FROM labs WHERE id = ?", (submission.lab_id,))
        lab_data = cursor.fetchone()
        
        if not lab_data:
            raise HTTPException(status_code=404, detail="靶机未找到")

        student_writeup = submission.answers.get("student_writeup", "未提供内容")
        
        # 💡 改进 2：微调 Prompt，让考官结合终端日志进行毒舌打击
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

        # 💡 改进 3：全量物料注入！阅卷官终于看到了完整的试卷！
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

        response = client.chat.completions.create(
            model=AI_MODEL,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt}
            ],
            temperature=0.1, # 低温保证评分的一致性
            max_tokens=2000,
            response_format={"type": "json_object"}
        )
        
        # 💡 改进 4：防崩溃装甲，用正则清洗脏字符
        raw_json_str = response.choices[0].message.content
        clean_json_str = re.sub(r'[\x00-\x08\x0b\x0c\x0e-\x1f]', '', raw_json_str)
        ai_report = json.loads(clean_json_str, strict=False)

        # 💡 整合：保存战报时，将 operator_name (username) 一并存入数据库
        cursor.execute('''
            INSERT INTO submissions (lab_id, operator_name, student_writeup, report)
            VALUES (?, ?, ?, ?)
        ''', (submission.lab_id, submission.username, student_writeup, json.dumps(ai_report, ensure_ascii=False)))
        conn.commit()

        return ai_report

    except json.JSONDecodeError as e:
        print(f"JSON 解析失败: {e}\n原始数据: {raw_json_str}")
        raise HTTPException(status_code=500, detail="AI 返回了无效的成绩单格式")
    except HTTPException:
        raise
    except Exception as e:
        print(f"判卷异常: {e}")
        raise HTTPException(status_code=500, detail=f"AI 判卷通信故障: {str(e)}")
    finally:
        # 💡 改进 5：防御性编程，无论成功还是异常，绝对释放数据库连接锁！
        if conn:
            conn.close()

@app.get("/api/history")
async def get_history(username: str, page: int = Query(1, ge=1), page_size: int = Query(15, ge=1, le=100)):
    """带平均分勋章的历史战报查询，支持分页，且仅拉取当前用户的记录"""
    conn = get_db_connection()
    try:
        cursor = conn.cursor()
        # 总数查询
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
# 4. 错题本 API
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
async def add_bookmark(item: BookmarkItem):
    """收藏一道错题到错题本"""
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
async def get_bookmarks(username: str):
    """获取用户错题本"""
    conn = get_db_connection()
    try:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT * FROM bookmarks WHERE operator_name = ? ORDER BY timestamp DESC",
            (username,)
        )
        rows = cursor.fetchall()
        result = []
        for row in rows:
            result.append({
                "id": row["id"],
                "lab_id": row["lab_id"],
                "question_text": row["question_text"],
                "question_focus": row["question_focus"],
                "missed_insights": json.loads(row["missed_insights"]),
                "feedback": row["feedback"],
                "score": row["score"],
                "timestamp": row["timestamp"],
            })
        return result
    finally:
        conn.close()

@app.delete("/api/bookmarks/{bookmark_id}")
async def delete_bookmark(bookmark_id: int, username: str):
    """删除一条错题记录（只能删自己的）"""
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
async def export_bookmarks(username: str):
    """将用户错题本导出为 Markdown 格式文本"""
    conn = get_db_connection()
    try:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT * FROM bookmarks WHERE operator_name = ? ORDER BY timestamp DESC",
            (username,)
        )
        rows = cursor.fetchall()
        if not rows:
            raise HTTPException(status_code=404, detail="错题本为空")

        lines = [
            f"# PaperLab 错题本 — {username}",
            f"> 导出时间：{__import__('datetime').datetime.now().strftime('%Y-%m-%d %H:%M')}",
            f"> 共 {len(rows)} 条错题记录",
            "",
        ]
        for i, row in enumerate(rows, 1):
            missed = json.loads(row["missed_insights"])
            lines += [
                f"---",
                f"## {i}. {row['question_text']}",
                f"",
                f"**靶机**：`{row['lab_id']}`　　**得分**：{row['score']}/10　　**时间**：{row['timestamp']}",
                f"",
                f"**考点 Focus**：{row['question_focus']}",
                f"",
                f"**AI 点评**：",
                f"",
                f"> {row['feedback']}",
                f"",
            ]
            if missed:
                lines.append("**遗漏的核心知识点**：")
                for m in missed:
                    lines.append(f"- `{m}`")
                lines.append("")

        md_content = "\n".join(lines)
        from fastapi.responses import Response
        return Response(
            content=md_content.encode("utf-8"),
            media_type="text/markdown; charset=utf-8",
            headers={"Content-Disposition": f'attachment; filename="paperlab_wrongbook_{username}.md"'}
        )
    finally:
        conn.close()


# ==========================================
# 6. MD 文件上传 → 实时裂变生成（SSE）
# ==========================================
import random

MAX_UPLOAD_SIZE = 5 * 1024 * 1024  # 5 MB

@app.post("/api/upload_and_build")
async def upload_and_build(request: Request, file: UploadFile = File(...), derive_count: int = 3, enable_quality_check: bool = True):
    """
    接收上传的 .md 文件，解析其中所有靶机母体，
    以 SSE 流（text/event-stream）实时推送每台靶机的生成进度。
    前端通过 EventSource 接收。
    """
    if not file.filename.endswith(".md"):
        raise HTTPException(status_code=400, detail="只支持 .md 格式文件")

    # 文件大小限制：先读 header，再限制读取字节数
    content_bytes = await file.read(MAX_UPLOAD_SIZE + 1)
    if len(content_bytes) > MAX_UPLOAD_SIZE:
        raise HTTPException(status_code=413, detail=f"文件超过最大限制 {MAX_UPLOAD_SIZE // 1024 // 1024} MB，请拆分后上传")
    try:
        md_text = content_bytes.decode("utf-8")
    except UnicodeDecodeError:
        md_text = content_bytes.decode("gbk", errors="replace")

    machines = parse_markdown_text_to_machines(md_text)
    if not machines:
        raise HTTPException(status_code=400, detail="未在文件中找到任何 ## 标题分隔的靶机母体")

    global_used_names = get_all_used_machine_names(DB_FILE)

    async def event_stream():
        total_machines = len(machines)
        total_variants = total_machines * derive_count
        done = 0

        yield f"data: {json.dumps({'type': 'start', 'total': total_variants, 'machines': total_machines}, ensure_ascii=False)}\n\n"

        for original_id, wp_text in machines.items():
            yield f"data: {json.dumps({'type': 'machine_start', 'machine': original_id, 'derive_count': derive_count}, ensure_ascii=False)}\n\n"

            for variant_idx in range(derive_count):
                # 在线程池中运行同步 LLM 调用，避免阻塞事件循环
                loop = asyncio.get_running_loop()
                success, new_name, lab_data, error = await loop.run_in_executor(
                    None,
                    lambda oid=original_id, vi=variant_idx: generate_single_variant(
                        client, AI_MODEL, wp_text, oid, vi,
                        global_used_names, DB_FILE, enable_quality_check
                    )
                )
                done += 1
                progress = round(done / total_variants * 100)

                if success:
                    yield f"data: {json.dumps({'type': 'variant_ok', 'machine': new_name, 'domain': lab_data.get('domain'), 'difficulty': lab_data.get('difficulty'), 'done': done, 'total': total_variants, 'progress': progress}, ensure_ascii=False)}\n\n"
                else:
                    yield f"data: {json.dumps({'type': 'variant_fail', 'original': original_id, 'variant_idx': variant_idx, 'error': error, 'done': done, 'total': total_variants, 'progress': progress}, ensure_ascii=False)}\n\n"

                await asyncio.sleep(0)  # 让出事件循环，保持 SSE 畅通

        yield f"data: {json.dumps({'type': 'done', 'total': total_variants, 'done': done}, ensure_ascii=False)}\n\n"

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"}
    )


# ==========================================
# 7. SM-2 间隔重复复盘 API
# ==========================================

def _sm2_update(easiness: float, interval: int, repetitions: int, score: int):
    """
    SM-2 算法核心计算。
    score: 0-5（由前端将 0-10 分折算为 0-5 传入，或后端折算）
    返回: (new_easiness, new_interval, new_repetitions, days_until_next)
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

    easiness = max(1.3, easiness + 0.1 - (5 - score) * (0.08 + (5 - score) * 0.02))
    return round(easiness, 2), interval, repetitions


class SM2ReviewItem(BaseModel):
    username: str
    lab_id: str
    question_idx: int
    question_text: str
    score_10: int  # 前端传 0-10 的原始分，后端折算为 SM-2 的 0-5


@app.post("/api/sm2/review")
async def sm2_review(item: SM2ReviewItem):
    """提交一道题的复盘评分，更新 SM-2 调度"""
    score_5 = round(item.score_10 / 2)  # 10分制 → 5分制
    conn = get_db_connection()
    try:
        cursor = conn.cursor()
        # 读取现有记录
        cursor.execute(
            "SELECT easiness, interval, repetitions FROM sm2_schedule WHERE operator_name=? AND lab_id=? AND question_idx=?",
            (item.username, item.lab_id, item.question_idx)
        )
        row = cursor.fetchone()
        easiness = row["easiness"] if row else 2.5
        interval = row["interval"] if row else 1
        repetitions = row["repetitions"] if row else 0

        new_e, new_i, new_r = _sm2_update(easiness, interval, repetitions, score_5)

        cursor.execute('''
            INSERT INTO sm2_schedule (operator_name, lab_id, question_idx, question_text, easiness, interval, repetitions, next_review, last_score)
            VALUES (?, ?, ?, ?, ?, ?, ?, date('now', ? || ' days'), ?)
            ON CONFLICT(operator_name, lab_id, question_idx) DO UPDATE SET
                easiness=excluded.easiness,
                interval=excluded.interval,
                repetitions=excluded.repetitions,
                next_review=excluded.next_review,
                last_score=excluded.last_score
        ''', (
            item.username, item.lab_id, item.question_idx, item.question_text,
            new_e, new_i, new_r, str(new_i), item.score_10
        ))
        conn.commit()
        return {"status": "ok", "next_review_in_days": new_i, "easiness": new_e, "repetitions": new_r}
    finally:
        conn.close()


@app.get("/api/sm2/today")
async def sm2_today(username: str):
    """返回今日需要复盘的题目列表（next_review <= today），含对应考点 focus"""
    conn = get_db_connection()
    try:
        cursor = conn.cursor()
        cursor.execute('''
            SELECT s.id, s.lab_id, s.question_idx, s.question_text,
                   s.easiness, s.interval, s.repetitions, s.next_review, s.last_score,
                   l.os, l.difficulty, l.domain, l.questions AS questions_json
            FROM sm2_schedule s
            LEFT JOIN labs l ON s.lab_id = l.id
            WHERE s.operator_name = ? AND s.next_review <= date('now')
            ORDER BY s.next_review ASC
        ''', (username,))
        rows = cursor.fetchall()
        result = []
        for row in rows:
            card = dict(row)
            # 从 labs.questions JSON 取出对应题目的 focus 字段
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
async def sm2_stats(username: str):
    """返回用户 SM-2 整体进度统计"""
    conn = get_db_connection()
    try:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT COUNT(*) as total FROM sm2_schedule WHERE operator_name=?", (username,)
        )
        total = cursor.fetchone()["total"]
        cursor.execute(
            "SELECT COUNT(*) as due FROM sm2_schedule WHERE operator_name=? AND next_review <= date('now')", (username,)
        )
        due = cursor.fetchone()["due"]
        cursor.execute(
            "SELECT AVG(last_score) as avg_score FROM sm2_schedule WHERE operator_name=?", (username,)
        )
        avg_row = cursor.fetchone()
        avg_score = round(avg_row["avg_score"] or 0, 1)
        return {"total_cards": total, "due_today": due, "avg_last_score": avg_score}
    finally:
        conn.close()


# ==========================================
# 5. 个人统计面板 API + 排行榜
# ==========================================

@app.get("/api/leaderboard")
async def get_leaderboard(limit: int = Query(20, ge=1, le=100)):
    """
    全平台排行榜：按平均分降序，平均分相同则按总次数降序。
    每个用户只统计有效（含 question_feedback）的提交。
    """
    conn = get_db_connection()
    try:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT operator_name, report FROM submissions ORDER BY timestamp ASC"
        )
        rows = cursor.fetchall()

        user_data: dict[str, dict] = {}
        for row in rows:
            name = row["operator_name"]
            try:
                report = json.loads(row["report"])
            except (json.JSONDecodeError, TypeError):
                continue
            scores = [q["score"] for q in report.get("question_feedback", []) if isinstance(q.get("score"), (int, float))]
            if not scores:
                continue
            avg = sum(scores) / len(scores)
            if name not in user_data:
                user_data[name] = {"total": 0, "score_sum": 0.0}
            user_data[name]["total"] += 1
            user_data[name]["score_sum"] += avg

        board = []
        for name, d in user_data.items():
            board.append({
                "operator_name": name,
                "avg_score": round(d["score_sum"] / d["total"], 1),
                "total_submissions": d["total"],
            })

        board.sort(key=lambda x: (-x["avg_score"], -x["total_submissions"]))
        for i, entry in enumerate(board, 1):
            entry["rank"] = i

        return board[:limit]
    finally:
        conn.close()


@app.get("/api/stats")
async def get_stats(username: str):
    """返回用户个人统计数据：各 Domain 平均分、Tag 维度分析、总体趋势"""
    conn = get_db_connection()
    try:
        cursor = conn.cursor()

        # 总提交数
        cursor.execute(
            "SELECT COUNT(*) as total FROM submissions WHERE operator_name = ?",
            (username,)
        )
        total = cursor.fetchone()["total"]
        if total == 0:
            return {"total": 0, "avg_score": 0, "domain_stats": [], "tag_stats": [], "trend": []}

        # 拉取必要字段（report 用于解析分数，tags 含 JSON）
        cursor.execute(
            "SELECT s.lab_id, s.report, s.timestamp, l.domain, l.tags "
            "FROM submissions s LEFT JOIN labs l ON s.lab_id = l.id "
            "WHERE s.operator_name = ? ORDER BY s.timestamp ASC",
            (username,)
        )
        rows = cursor.fetchall()

        domain_data = {}
        tag_data = {}
        trend = []
        all_avgs = []

        for row in rows:
            try:
                report = json.loads(row["report"])
            except (json.JSONDecodeError, TypeError):
                continue
            scores = [q["score"] for q in report.get("question_feedback", []) if isinstance(q.get("score"), (int, float))]
            if not scores:
                continue
            avg = round(sum(scores) / len(scores), 1)
            all_avgs.append(avg)

            domain = row["domain"] or "Unknown"
            domain_data.setdefault(domain, []).append(avg)

            try:
                tags = json.loads(row["tags"]) if row["tags"] else []
            except (json.JSONDecodeError, TypeError):
                tags = []
            for tag in tags:
                tag_data.setdefault(tag, []).append(avg)

            trend.append({"timestamp": row["timestamp"], "avg_score": avg, "lab_id": row["lab_id"]})

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