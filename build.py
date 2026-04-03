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
import sqlite3
from openai import OpenAI

# ==========================================
# 1. 核心配置区 (从 lab_generator 统一加载)
# ==========================================
from lab_generator import load_config

_cfg = load_config()

client = OpenAI(
    api_key=_cfg["api_key"],
    base_url=_cfg["base_url"],
    timeout=120.0
)
AI_MODEL = _cfg["model"]

MD_DIR = "md"
DB_FILE = "paperlab.db"
TEST_MODE_LIMIT = 50

# 💡 衍生倍率：一份真实的 MD 笔记，裂变出几个不同的变种靶机？
DERIVE_COUNT = 3

# 💡 留空则全量编译 md 目录下所有未编译的笔记；填入 ID 列表则只编译指定的笔记
TARGET_LABS = []

# 💡 是否启用质量评分过滤（会额外消耗一次 LLM 调用）
ENABLE_QUALITY_CHECK = False

# ==========================================
# 2. 共用逻辑从 lab_generator 导入
# ==========================================
from lab_generator import (
    parse_markdown_to_machines,
    get_all_used_machine_names,
    generate_single_variant,
)

# ==========================================
# 3. 数据库初始化
# ==========================================
def auto_init_db():
    conn = sqlite3.connect(DB_FILE)
    conn.execute("PRAGMA journal_mode=WAL")
    cursor = conn.cursor()
    cursor.execute('''CREATE TABLE IF NOT EXISTS labs (
        id TEXT PRIMARY KEY, os TEXT, difficulty TEXT, domain TEXT,
        tags TEXT, context TEXT, questions TEXT, focus_points TEXT
    )''')
    cursor.execute('''CREATE TABLE IF NOT EXISTS submissions (
        id INTEGER PRIMARY KEY AUTOINCREMENT, lab_id TEXT,
        operator_name TEXT, student_writeup TEXT, report TEXT,
        timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
    )''')
    cursor.execute('''CREATE TABLE IF NOT EXISTS build_history (
        original_name TEXT PRIMARY KEY, new_name TEXT
    )''')
    conn.commit()
    conn.close()

def get_existing_labs():
    try:
        conn = sqlite3.connect(DB_FILE)
        cursor = conn.cursor()
        cursor.execute("SELECT original_name FROM build_history")
        rows = cursor.fetchall()
        conn.close()
        return [row[0] for row in rows]
    except Exception:
        return []

# ==========================================
# 4. 变异裂变核心逻辑（调用 lab_generator 共用函数）
# ==========================================
def build_pro_database():
    auto_init_db()
    existing_labs = get_existing_labs()
    global_used_names = get_all_used_machine_names(DB_FILE)

    if not os.path.exists(MD_DIR):
        print(f”⚠️ 目录 {MD_DIR} 不存在，请创建并在其中放入 .md 笔记文件。”)
        return

    compiled_count = 0
    for filename in os.listdir(MD_DIR):
        if not filename.endswith(“.md”):
            continue
        filepath = os.path.join(MD_DIR, filename)

        machines_dict = parse_markdown_to_machines(filepath)

        for original_id, wp_text in machines_dict.items():
            if TARGET_LABS and original_id not in TARGET_LABS:
                continue

            # 如果变种 0 已经存在，说明这个母体已经被处理过了
            base_history_check = f”{original_id}_v0”
            if not TARGET_LABS and base_history_check in existing_labs:
                continue

            if compiled_count >= TEST_MODE_LIMIT:
                return

            print(f”\n==================================================”)
            print(f”⚙️  提取母体基因 [{original_id}]，准备执行 {DERIVE_COUNT} 次变异衍生...”)

            for variant_idx in range(DERIVE_COUNT):
                print(f”\n   🔬 正在培育变种 {variant_idx + 1}/{DERIVE_COUNT}...”)

                success, new_name, lab_data, error = generate_single_variant(
                    client, AI_MODEL, wp_text, original_id, variant_idx,
                    global_used_names, DB_FILE, ENABLE_QUALITY_CHECK
                )

                if success:
                    questions_list = lab_data.get('questions', [])
                    first_q_text = questions_list[0].get('text', '未能提取到题目')[:45] if questions_list else '无题目'

                    print(f”   ✅ 成功蜕变为新靶机: [{new_name}]”)
                    print(f”      🎯 环境: {lab_data.get('domain')} | 难度: {lab_data.get('difficulty')} | 标签: {lab_data.get('tags')}”)
                    print(f”      📝 任务数: {len(questions_list)} 个 (已确保≥3)”)
                    print(f”      🇨🇳 首题检测: {first_q_text}...”)
                else:
                    print(f”   ❌ 变种 {variant_idx + 1} 培育失败: {error}”)

            compiled_count += 1

if __name__ == “__main__”:
    build_pro_database()