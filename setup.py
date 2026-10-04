#!/usr/bin/env python3
# -*- coding: utf-8 -*-
#
# Project: PaperLab - AI Automated OSCP Lab Generator
# Author: tw1t
# Copyright 2026 tw1t
# SPDX-License-Identifier: Apache-2.0
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
         S E T U P   W I Z A R D   v1.2
"""

# 服务端点。base URL 是稳定的（不像模型名会频繁变动），因此这里维护清单；
# 模型名一律在运行时向端点拉取（见 fetch_available_models）。
# 注意：这些端点都提供 OpenAI 兼容的 /chat/completions，PaperLab 依赖这一点。
SUPPORTED_ENDPOINTS = [
    ("1", "https://api.deepseek.com/v1",            "DeepSeek 官方 API"),
    ("2", "https://api.openai.com/v1",              "OpenAI 官方 API"),
    ("3", "https://open.bigmodel.cn/api/paas/v4/",  "智谱 GLM 官方 API"),
    ("4", "https://api.x.ai/v1",                    "xAI Grok 官方 API"),
    ("5", "https://api.anthropic.com/v1",           "Anthropic Claude（经其 OpenAI 兼容层）"),
    ("6", "custom",                                  "手动输入自定义 Base URL"),
]

# 拉取失败时，指引用户去对应控制台查可用模型名。仅作兜底提示。
ENDPOINT_CONSOLE = {
    "https://api.deepseek.com/v1": "https://platform.deepseek.com",
    "https://api.openai.com/v1": "https://platform.openai.com",
    "https://open.bigmodel.cn/api/paas/v4/": "https://open.bigmodel.cn",
    "https://api.x.ai/v1": "https://console.x.ai",
    "https://api.anthropic.com/v1": "https://console.anthropic.com",
}

# 模型名会随服务商迭代下线或改名（DeepSeek 的 deepseek-chat / deepseek-reasoner
# 已于 2026-07 退役），因此这里不再维护硬编码清单，改为向端点实时拉取。
# 下列关键词用于从返回结果中剔除明显不能用于对话的模型（PaperLab 走 chat.completions）。
NON_CHAT_HINTS = (
    "embedding", "embed-", "whisper", "-tts", "tts-", "dall-e", "image",
    "moderation", "audio", "realtime", "transcribe", "sora", "video",
    "-search-", "codex", "rerank",
)

# 实验/预览版排在正式版之后：它们不适合作为默认选择。
EXPERIMENTAL_HINTS = ("exp", "experimental", "preview", "beta", "alpha", "snapshot")


def _is_experimental(model_id):
    low = model_id.lower()
    return any(h in low for h in EXPERIMENTAL_HINTS)


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

    print_green(f"  [+] API Key 已记录: {api_key[:6]}{'*' * max(len(api_key) - 8, 0)}{api_key[-2:]}")
    return api_key


def input_base_url():
    """引导选择 API Base URL"""
    print()
    print_cyan("━" * 55)
    print_cyan(" STEP 2 / 3  —  API 服务端点配置")
    print_cyan("━" * 55)
    print("  请选择您的 AI 服务端点：")
    print()
    for num, url, desc in SUPPORTED_ENDPOINTS:
        print(f"   [{num}] {desc}")
    print()

    last = str(len(SUPPORTED_ENDPOINTS))
    while True:
        choice = input(f"  请选择 [1-{last}，默认 1] > ").strip() or "1"
        matched = [e for e in SUPPORTED_ENDPOINTS if e[0] == choice]
        if not matched:
            print_red(f"  [!] 无效选项，请输入 1-{last}。")
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


MAX_LISTED_MODELS = 30

# 拉取失败的原因分类。必须区分开，否则用户无法判断该去修什么。
AUTH_ERROR_KINDS = ("auth",)


def fetch_available_models(api_key, base_url):
    """
    向服务端点拉取可用对话模型列表（OpenAI 兼容的 GET /models）。

    注意：/models 是**鉴权接口**，必须带有效 API Key（实测 DeepSeek 不带 key 返回 401）。

    返回 (models, kind, detail)：
      成功 -> (list, None, None)
      失败 -> (None, kind, detail)，kind 用于给出针对性提示：
        auth     凭据无效        —— 该修 API Key
        network  连不上           —— 该查网络/地址
        notfound 端点无此接口      —— 该手动输入模型名
        rate     被限流
        other    其他
    """
    try:
        from openai import OpenAI
        import openai as _openai
    except ImportError:
        return None, "other", "未安装 openai 库，请先执行 pip install -r requirements.txt"

    try:
        client = OpenAI(api_key=api_key, base_url=base_url, timeout=15.0)
        resp = client.models.list()
    except _openai.AuthenticationError:
        return None, "auth", "API Key 无效或已过期（服务端返回 401）"
    except _openai.PermissionDeniedError:
        return None, "auth", "该 API Key 无权访问此端点（403）"
    except _openai.NotFoundError:
        return None, "notfound", "该端点未提供 /models 接口（404）"
    except _openai.RateLimitError:
        return None, "rate", "请求过于频繁（429）"
    except _openai.APIConnectionError:
        return None, "network", f"无法连接到 {base_url}（网络不通或地址有误）"
    except _openai.InternalServerError as e:
        # 5xx 多来自网关/代理：例如设置了 http_proxy 时，
        # 对 localhost 的请求会被送去代理并可能返回 502。
        proxy = (os.environ.get("https_proxy") or os.environ.get("http_proxy")
                 or os.environ.get("HTTPS_PROXY") or os.environ.get("HTTP_PROXY"))
        hint = f"；当前检测到代理 {proxy}，若该地址无需走代理请先取消代理环境变量" if proxy else ""
        return None, "network", f"服务端/网关返回 5xx（{type(e).__name__}）{hint}"
    except Exception as e:
        # 少数服务商不用标准的 401 表达鉴权失败
        # （实测 xAI 对无效 key 返回 400 + "Incorrect API key provided"），
        # 因此再按错误文案兜一层，避免把「key 错」误报成「其他错误」。
        low = str(e).lower()
        if any(k in low for k in ("api key", "api_key", "apikey",
                                  "unauthorized", "authentication", "invalid token")):
            return None, "auth", f"API Key 无效或未被接受（{type(e).__name__}）"
        return None, "other", f"{type(e).__name__}: {e}"

    ids = []
    for m in (getattr(resp, "data", None) or []):
        mid = getattr(m, "id", None)
        if isinstance(mid, str) and mid.strip():
            ids.append(mid.strip())
    if not ids:
        return None, "other", "端点返回了空的模型列表"

    chat = [i for i in ids if not any(h in i.lower() for h in NON_CHAT_HINTS)]
    pool = set(chat or ids)
    # 正式版在前（组内保持字母序），实验版靠后
    return sorted(pool, key=lambda m: (_is_experimental(m), m)), None, None


def _input_model_manually():
    print()
    while True:
        mid = input("  模型名 > ").strip()
        if mid:
            print_green(f"  [+] 模型: {mid}")
            return mid
        print_red("  [!] 模型名不能为空。")


def input_model(api_key, base_url):
    """
    引导选择模型。返回 (model_name, need_retry_key)。
    need_retry_key 为 True 表示凭据有问题、应当回到第一步重输 API Key。
    """
    print()
    print_cyan("━" * 55)
    print_cyan(" STEP 3 / 3  —  AI 模型配置")
    print_cyan("━" * 55)
    print("  正在获取该端点可用的模型列表…")
    models, kind, detail = fetch_available_models(api_key, base_url)

    if not models:
        print_red(f"  [!] 无法获取模型列表：{detail}")
        if kind in AUTH_ERROR_KINDS:
            # 凭据无效时必须点破：否则用户手动填个模型名也能走完向导，
            # 却会在生成靶机时才发现 key 是错的。
            print_yellow("      这一项必须修好才能使用，否则后续生成/判卷都会失败。")
            ans = input("  是否重新输入 API Key？[Y/n] > ").strip().lower()
            if ans != "n":
                return None, True
        elif kind == "network":
            print_yellow("      请检查网络，或确认该端点地址是否正确。")
        elif kind == "notfound":
            print_yellow("      部分中转/自建端点不实现该接口，请手动输入模型名。")
        console = ENDPOINT_CONSOLE.get(base_url)
        if console:
            print_cyan(f"      可在控制台查看可用模型名：{console}")
        return _input_model_manually(), False

    shown = models[:MAX_LISTED_MODELS]
    print_green(f"  [+] 获取到 {len(models)} 个可用模型，请选择用于靶机生成与评分的模型：")
    print()
    for i, mid in enumerate(shown, 1):
        note = ""
        if i == 1:
            note = "   ← 默认"
        elif _is_experimental(mid):
            note = "   (实验版)"
        print(f"   [{i}] {mid}{note}")
    if len(models) > len(shown):
        print_cyan(f"   … 其余 {len(models) - len(shown)} 个未显示")
    print("   [0] 手动输入模型名")
    print()

    while True:
        raw = input(f"  请选择 [0-{len(shown)}，默认 1] > ").strip() or "1"
        if raw == "0":
            return _input_model_manually(), False
        if raw.isdigit() and 1 <= int(raw) <= len(shown):
            mid = shown[int(raw) - 1]
            print_green(f"  [+] 模型: {mid}")
            return mid, False
        print_red(f"  [!] 无效选项，请输入 0-{len(shown)}。")


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
    print_green("    python build.py                   # 生成/扩充靶机库（可选，仓库自带示例库）")
    print_green("    python main.py                    # 启动 PaperLab 训练平台")
    print()
    print_cyan("  可选环境变量：")
    print("    PAPERLAB_ORIGINS         允许跨域的来源，默认仅本机。局域网共享时设置，例：")
    print("                             PAPERLAB_ORIGINS=http://192.168.1.10:8000")
    print("    PAPERLAB_ENABLE_DOCS     设为 1 可打开 /docs 接口文档（默认关闭）")
    print("    PAPERLAB_DB              自定义数据库路径（默认 paperlab.db）")
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

    # 凭据无效时回到第一步重输，避免用户一路配完却在生成靶机时才失败
    while True:
        api_key = input_api_key()
        base_url = input_base_url()
        model, retry_key = input_model(api_key, base_url)
        if not retry_key:
            break
        print()
        print_yellow("  请重新输入 API Key（DeepSeek 用户可在 https://platform.deepseek.com 查看）。")
        print()

    update_gitignore()
    write_config(api_key, base_url, model)


if __name__ == "__main__":
    main()
