#!/usr/bin/env python3
# -*- coding: utf-8 -*-
#
# Project: PaperLab - AI Automated OSCP Lab Generator
# Author: tw1t
# Copyright 2026 tw1t
# SPDX-License-Identifier: Apache-2.0
"""
口令哈希与校验。

从 main.py 抽出来的理由：这部分是纯函数（不碰数据库、不依赖 FastAPI），
既可以被 main 使用，也可以被单独的测试直接覆盖 —— 之前它跟 22 个路由挤在
同一个文件里，改哈希策略要在一个 1200 行的文件里翻。

设计要点：

1. **scrypt + 每用户随机盐**。旧的实现是 `sha256("paperlab::" + raw)`：
   固定盐（彩虹表建一次打穿全库）加快速哈希（GPU 每秒几十亿次）。
2. **向后兼容**。老账号的 sha256 哈希仍然能校验，并且在登录成功时自动升级成
   scrypt —— 用户无感，不用重置口令。
3. **常量时间比较**。全部走 hmac.compare_digest，避免时序侧信道。
"""

import base64
import hashlib
import hmac
import os

from pydantic import BaseModel

SCRYPT_N = 2 ** 14
SCRYPT_R = 8
SCRYPT_P = 1
SCRYPT_DKLEN = 32
SCRYPT_PREFIX = "scrypt$"


def b64(raw: bytes) -> str:
    return base64.b64encode(raw).decode("ascii")


def hash_password(raw: str) -> str:
    """
    生成口令哈希：`scrypt$n$r$p$salt_b64$hash_b64`。

    参数随串存储，日后调强度不影响老数据。
    """
    salt = os.urandom(16)
    dk = hashlib.scrypt(raw.encode("utf-8"), salt=salt,
                        n=SCRYPT_N, r=SCRYPT_R, p=SCRYPT_P, dklen=SCRYPT_DKLEN)
    return f"{SCRYPT_PREFIX}{SCRYPT_N}${SCRYPT_R}${SCRYPT_P}${b64(salt)}${b64(dk)}"


def legacy_hash_password(raw: str) -> str:
    """旧的固定盐 SHA-256，**只用于兼容已有账号**，不再用于写入新口令。"""
    return hashlib.sha256(("paperlab::" + raw).encode("utf-8")).hexdigest()


def needs_rehash(stored: str) -> bool:
    """老格式（没有 scrypt$ 前缀）需要在校验通过后顺手升级。"""
    return not (stored or "").startswith(SCRYPT_PREFIX)


def verify_password(raw: str, stored: str) -> bool:
    """兼容两种格式的校验。"""
    if not stored:
        return False
    if stored.startswith(SCRYPT_PREFIX):
        try:
            _, n_s, r_s, p_s, salt_b64, hash_b64 = stored.split("$")
            expected = base64.b64decode(hash_b64)
            dk = hashlib.scrypt(
                raw.encode("utf-8"),
                salt=base64.b64decode(salt_b64),
                n=int(n_s), r=int(r_s), p=int(p_s), dklen=len(expected),
            )
            return hmac.compare_digest(b64(dk), hash_b64)
        except (ValueError, TypeError):
            return False
    return hmac.compare_digest(legacy_hash_password(raw), stored)


class AuthRequest(BaseModel):
    name: str
    password: str | None = None
