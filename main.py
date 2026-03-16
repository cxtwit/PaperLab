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
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel
from openai import OpenAI
from fastapi.middleware.cors import CORSMiddleware

# ==========================================
# 1. 核心配置 (DeepSeek API)
# ==========================================
client = OpenAI(
    api_key="YOUR_API_KEY_HERE",
    base_url="https://api.deepseek.com/v1"
)

app = FastAPI(title="PaperLab - Pro Examiner Edition")

app = FastAPI(docs_url=None, redoc_url=None)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

DB_FILE = "paperlab.db"

# ==========================================
# 2. 数据库底层查询工具
# ==========================================
def get_db_connection():
    conn = sqlite3.connect(DB_FILE)
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
async def list_labs():
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        # 💡 整合：读取时包含 domain 字段，支持前端的下拉框筛选
        cursor.execute("SELECT id, os, difficulty, domain FROM labs")
        rows = cursor.fetchall()
        conn.close()
        return [dict(row) for row in rows]
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Database Error: {str(e)}")

@app.get("/api/get_lab/{lab_id}")
async def get_lab_detail(lab_id: str):
    """适配 Pro 版 Schema，包含 OS、难度、领域和标签"""
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM labs WHERE id = ?", (lab_id,))
    row = cursor.fetchone()
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
        "focus_points": row["focus_points"]
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
            model="deepseek-chat",
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
async def get_history(username: str):
    """带平均分勋章的历史战报查询，且仅拉取当前用户的记录"""
    conn = get_db_connection()
    try:
        cursor = conn.cursor()
        # 💡 整合：通过 operator_name = ? 过滤，确保别人看不到你的战报
        cursor.execute("SELECT * FROM submissions WHERE operator_name = ? ORDER BY timestamp DESC LIMIT 15", (username,))
        rows = cursor.fetchall()
        
        history_list = []
        for row in rows:
            report = json.loads(row["report"])
            scores = [q["score"] for q in report.get("question_feedback", [])]
            avg = round(sum(scores) / len(scores), 1) if scores else 0
            
            history_list.append({
                "id": row["id"],
                "lab_id": row["lab_id"],
                "timestamp": row["timestamp"],
                "avg_score": avg,
                "summary": report["evaluation_report"]["executive_summary"][:50] + "...",
                "report": report
            })
        return history_list
    finally:
        # 安全关闭连接
        conn.close()