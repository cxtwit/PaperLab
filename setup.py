#!/usr/bin/env python3
# -*- coding: utf-8 -*-
#
# Project: PaperLab - AI Automated OSCP Lab Generator
# Author: tw1t
#
# 首次部署配置向导 - 运行此脚本完成初始化配置
# First-time setup wizard - run this script to initialize your configuration

import json
import os
import sys

CONFIG_FILE = "config.json"

BANNER = r"""
  ____                        _           _
 |  _ \ __ _ _ __   ___ _ __| |    __ _| |__
 | |_) / _` | '_ \ / _ \ '__| |   / _` | '_ \
 |  __/ (_| | |_) |  __/ |  | |__| (_| | |_) |
 |_|   \__,_| .__/ \___|_|  |_____\__,_|_.__/
             |_|
         S E T U P   W I Z A R D   v1.0
"""

SUPPORTED_MODELS = [
    ("1", "deepseek-chat",      "DeepSeek Chat (推荐，性价比最高)"),
    ("2", "deepseek-reasoner",  "DeepSeek Reasoner (R1，推理更强但更慢)"),
    ("3", "gpt-4o",             "OpenAI GPT-4o (需要 OpenAI API Key)"),
    ("4", "gpt-4o-mini",        "OpenAI GPT-4o Mini (更快更便宜)"),
    ("5", "custom",             "手动输入自定义模型名"),
]

SUPPORTED_ENDPOINTS = [
    ("1", "https://api.deepseek.com/v1",    "DeepSeek 官方 API (推荐)"),
    ("2", "https://api.openai.com/v1",      "OpenAI 官方 API"),
    ("3", "custom",                          "手动输入自定义 Base URL"),
]


def print_green(text):
    print(f"\033[92m{text}\033[0m")

def print_yellow(text):
    print(f"\033[93m{text}\033[0m")

def print_red(text):
    print(f"\033[91m{text}\033[0m")

def print_cyan(text):
    print(f"\033[96m{text}\033[0m")


def check_existing_config():
    """检查是否已有配置文件"""
    if os.path.exists(CONFIG_FILE):
        print_yellow(f"\n[!] 检测到已有配置文件 {CONFIG_FILE}")
        choice = input("    是否覆盖重新配置? [y/N] ").strip().lower()
        if choice != 'y':
            print_green("\n[+] 已保留现有配置，无需重新配置。")
            print_cyan(f"    提示：如需修改，请直接编辑 {CONFIG_FILE} 或重新运行 setup.py\n")
            sys.exit(0)
        print()


def input_api_key():
    """引导输入 API Key"""
    print_cyan("━" * 55)
    print_cyan(" STEP 1 / 3  —  API Key 配置")
    print_cyan("━" * 55)
    print("  请输入您的 AI 服务 API Key。")
    print("  (DeepSeek 用户请前往 https://platform.deepseek.com 获取)")
    print()

    while True:
        api_key = input("  API Key > ").strip()
        if not api_key:
            print_red("  [!] API Key 不能为空，请重新输入。")
            continue
        if api_key == "YOUR_API_KEY_HERE":
            print_red("  [!] 请输入真实的 API Key，不要使用占位符。")
            continue
        if len(api_key) < 8:
            print_red("  [!] API Key 看起来太短了，请确认是否正确。")
            continue
        break

    print_green(f"  [+] API Key 已记录: {api_key[:6]}{'*' * (len(api_key) - 8)}{api_key[-2:]}")
    return api_key


def input_base_url():
    """引导选择 API Base URL"""
    print()
    print_cyan("━" * 55)
    print_cyan(" STEP 2 / 3  —  API 服务端点配置")
    print_cyan("━" * 55)
    print("  请选择您的 AI 服务端点：")
    print()
    for num, _, desc in SUPPORTED_ENDPOINTS:
        print(f"   [{num}] {desc}")
    print()

    while True:
        choice = input("  请选择 [1-3，默认 1] > ").strip() or "1"
        matched = [e for e in SUPPORTED_ENDPOINTS if e[0] == choice]
        if not matched:
            print_red("  [!] 无效选项，请输入 1-3。")
            continue

        _, url, _ = matched[0]
        if url == "custom":
            while True:
                url = input("  请输入自定义 Base URL > ").strip()
                if url.startswith(("http://", "https://")):
                    break
                print_red("  [!] URL 必须以 http:// 或 https:// 开头。")

        print_green(f"  [+] 服务端点: {url}")
        return url


def input_model():
    """引导选择模型"""
    print()
    print_cyan("━" * 55)
    print_cyan(" STEP 3 / 3  —  AI 模型配置")
    print_cyan("━" * 55)
    print("  请选择用于靶机生成和评分的 AI 模型：")
    print()
    for num, model_id, desc in SUPPORTED_MODELS:
        print(f"   [{num}] {desc}")
        if num != "5":
            print(f"       模型 ID: {model_id}")
        print()

    while True:
        choice = input("  请选择 [1-5，默认 1] > ").strip() or "1"
        matched = [m for m in SUPPORTED_MODELS if m[0] == choice]
        if not matched:
            print_red("  [!] 无效选项，请输入 1-5。")
            continue

        _, model_id, _ = matched[0]
        if model_id == "custom":
            while True:
                model_id = input("  请输入自定义模型名称 > ").strip()
                if model_id:
                    break
                print_red("  [!] 模型名称不能为空。")

        print_green(f"  [+] 模型: {model_id}")
        return model_id


def write_config(api_key, base_url, model):
    """写入 config.json"""
    config = {
        "api_key": api_key,
        "base_url": base_url,
        "model": model
    }
    with open(CONFIG_FILE, "w", encoding="utf-8") as f:
        json.dump(config, f, indent=4, ensure_ascii=False)

    print()
    print_cyan("━" * 55)
    print_green(f"  [+] 配置已写入 {CONFIG_FILE}")
    print_green("  [+] 安装向导完成！")
    print_cyan("━" * 55)
    print()
    print("  下一步：")
    print_green("    python build.py     # 生成靶机数据库（首次使用必须）")
    print_green("    python main.py      # 启动 PaperLab 训练平台")
    print()
    print_yellow("  [!] 提示：config.json 包含您的 API Key，请勿上传至 Git 仓库！")
    print()


def update_gitignore():
    """确保 config.json 在 .gitignore 中"""
    gitignore_path = ".gitignore"
    config_entry = "config.json"

    if os.path.exists(gitignore_path):
        with open(gitignore_path, "r", encoding="utf-8") as f:
            content = f.read()
        if config_entry not in content.splitlines():
            with open(gitignore_path, "a", encoding="utf-8") as f:
                f.write(f"\n# PaperLab 本地配置（含 API Key，禁止提交）\n{config_entry}\n")
    else:
        with open(gitignore_path, "w", encoding="utf-8") as f:
            f.write(f"# PaperLab 本地配置（含 API Key，禁止提交）\n{config_entry}\n")


def main():
    print_green(BANNER)

    check_existing_config()

    api_key = input_api_key()
    base_url = input_base_url()
    model    = input_model()

    update_gitignore()
    write_config(api_key, base_url, model)


if __name__ == "__main__":
    main()
