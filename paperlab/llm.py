#!/usr/bin/env python3
# -*- coding: utf-8 -*-
#
# Project: PaperLab - AI Automated OSCP Lab Generator
# Author: tw1t
# Copyright 2026 tw1t
# SPDX-License-Identifier: Apache-2.0
"""
模型配置与 OpenAI 客户端。

从 main.py 抽出来的原因：生成（上传接口）、判卷（evaluate/hint）、健康检查
（healthz 要报当前模型名）三处都要用。留在 main 里的话，这些路由模块只能
反过来 import main，形成循环导入。

注意：import 时就会 load_config()，配置缺失直接退出（沿用原行为），
所以 tests/conftest.py 会先把工作目录切到临时目录并写好一份假配置。
"""

from openai import OpenAI

from lab_generator import load_config

CFG = load_config()
AI_MODEL = CFG["model"]
BASE_URL = CFG["base_url"]

client = OpenAI(
    api_key=CFG["api_key"],
    base_url=BASE_URL,
    timeout=120.0,
)
