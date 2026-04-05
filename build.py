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
import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
from openai import OpenAI

# ==========================================
# 1. 核心配置区 (从 lab_generator 统一加载)
# ==========================================
from lab_generator import (
    load_config,
    ensure_db,
    parse_markdown_to_machines,
    get_all_used_machine_names,
    generate_single_variant,
)

_cfg = load_config()

client = OpenAI(
    api_key=_cfg["api_key"],
    base_url=_cfg["base_url"],
    timeout=120.0
)
AI_MODEL = _cfg["model"]

MD_DIR = "md"
DB_FILE = "paperlab.db"

# ==========================================
# 2. 默认配置（可通过 CLI 参数覆盖）
# ==========================================
DEFAULT_DERIVE_COUNT = 3
DEFAULT_MAX_SOURCES = 50
DEFAULT_QUALITY_CHECK = False

# ==========================================
# 3. 数据库初始化
# ==========================================
def get_existing_labs():
    try:
        conn = sqlite3.connect(DB_FILE)
        conn.execute("PRAGMA journal_mode=WAL")
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
def build_pro_database(target_labs, derive_count, max_sources, enable_quality_check, max_workers):
    ensure_db(DB_FILE)
    existing_labs = get_existing_labs()
    global_used_names = get_all_used_machine_names(DB_FILE)

    if not os.path.exists(MD_DIR):
        print(f"⚠️ 目录 {MD_DIR} 不存在，请创建并在其中放入 .md 笔记文件。")
        return

    # 收集待处理的 (original_id, wp_text, variant_idx) 三元组
    tasks = []
    compiled_count = 0
    for filename in sorted(os.listdir(MD_DIR)):
        if not filename.endswith(".md"):
            continue
        filepath = os.path.join(MD_DIR, filename)
        machines_dict = parse_markdown_to_machines(filepath)

        for original_id, wp_text in machines_dict.items():
            if target_labs and original_id not in target_labs:
                continue
            base_history_check = f"{original_id}_v0"
            if not target_labs and base_history_check in existing_labs:
                continue
            if compiled_count >= max_sources:
                break

            print(f"\n==================================================")
            print(f"⚙️  提取母体基因 [{original_id}]，准备执行 {derive_count} 次变异衍生...")
            for variant_idx in range(derive_count):
                tasks.append((original_id, wp_text, variant_idx))
            compiled_count += 1

        if compiled_count >= max_sources:
            break

    if not tasks:
        print("✅ 没有需要编译的新母体。")
        return

    print(f"\n🚀 共 {len(tasks)} 个变种任务，并发线程数: {max_workers}\n")

    def run_task(args):
        original_id, wp_text, variant_idx = args
        return generate_single_variant(
            client, AI_MODEL, wp_text, original_id, variant_idx,
            global_used_names, DB_FILE, enable_quality_check
        ), original_id, variant_idx

    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {executor.submit(run_task, t): t for t in tasks}
        for future in as_completed(futures):
            (success, new_name, lab_data, error), original_id, variant_idx = future.result()
            if success:
                questions_list = lab_data.get('questions', [])
                first_q_text = questions_list[0].get('text', '未能提取到题目')[:45] if questions_list else '无题目'
                print(f"   ✅ [{original_id} v{variant_idx}] → [{new_name}] {lab_data.get('domain')} · {lab_data.get('difficulty')}")
                print(f"      📝 {len(questions_list)} 题  首题: {first_q_text}...")
            else:
                print(f"   ❌ [{original_id} v{variant_idx}] 失败: {error}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="PaperLab 靶机数据库生成器",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""示例:
  python build.py                          # 全量编译，默认配置
  python build.py --target HTB-Lame HTB-Blue  # 只编译指定靶机
  python build.py --derive 5              # 每台母体生成 5 个变种
  python build.py --workers 5 --quality   # 5 线程并发 + 开启质量过滤
  python build.py --max-sources 10        # 最多处理 10 台源机器
"""
    )
    parser.add_argument(
        "--target", nargs="*", default=[],
        metavar="HTB-NAME",
        help="指定要编译的母体 ID（如 HTB-Lame），留空则全量编译"
    )
    parser.add_argument(
        "--derive", type=int, default=DEFAULT_DERIVE_COUNT,
        metavar="N",
        help=f"每台母体生成的变种数（默认: {DEFAULT_DERIVE_COUNT}）"
    )
    parser.add_argument(
        "--max-sources", type=int, default=DEFAULT_MAX_SOURCES,
        metavar="N",
        help=f"最多处理的源机器数量（默认: {DEFAULT_MAX_SOURCES}）"
    )
    parser.add_argument(
        "--quality", action="store_true", default=DEFAULT_QUALITY_CHECK,
        help="启用质量评分过滤（会额外消耗一次 LLM 调用）"
    )
    parser.add_argument(
        "--workers", type=int, default=3,
        metavar="N",
        help="并发线程数（默认: 3，建议不超过 5 避免 API 限速）"
    )

    args = parser.parse_args()
    build_pro_database(
        target_labs=args.target,
        derive_count=args.derive,
        max_sources=args.max_sources,
        enable_quality_check=args.quality,
        max_workers=args.workers,
    )
