#!/usr/bin/env python3
# -*- coding: utf-8 -*-
#
# PaperLab — 共用的靶机 LLM 生成逻辑
# 供 build.py 批量编译和 main.py 上传裂变 API 共同调用
#
# Copyright 2026 tw1t
# SPDX-License-Identifier: Apache-2.0
import json
import os
import re
import sys
import time
import uuid
import random
import sqlite3
import threading

# ==========================================
# 0. 全局常量与配置
# ==========================================

DB_FILE = os.environ.get("PAPERLAB_DB", "paperlab.db")
CONFIG_FILE = "config.json"

# 写库串行锁：SQLite 并发写同一文件会抛 "database is locked" /
# "attempt to write a readonly database"，且重试无法自愈。勿删。
_DB_WRITE_LOCK = threading.RLock()

# 名称占用锁。保证「检查重名 → 占用」是原子操作，
# 避免并发线程同时通过去重判断后互相覆盖。
_NAME_LOCK = threading.RLock()

# Domain 白名单（与 Prompt 中的死锁列表保持严格一致）
ALLOWED_DOMAINS = [
    "Web Application",
    "Active Directory",
    "Network Services",
    "Linux Privilege Escalation",
    "Windows Privilege Escalation",
    "Internal Network",
    "Mixed",
]

ALLOWED_DIFFICULTIES = ["Easy", "Medium", "Hard"]

# Domain 归一时使用的关键词回退表（顺序敏感，先匹配先命中）
_DOMAIN_KEYWORD_FALLBACK = [
    ("active directory", "Active Directory"),
    ("kerberos", "Active Directory"),
    ("linux privilege", "Linux Privilege Escalation"),
    ("windows privilege", "Windows Privilege Escalation"),
    ("privilege escalation", "Linux Privilege Escalation"),
    ("internal network", "Internal Network"),
    ("pivot", "Internal Network"),
    ("network", "Network Services"),
    ("web", "Web Application"),
]

# 喂给模型的黑名单上限（按基名计）；唯一性由 _NAME_LOCK 保证，这里只为减少无用提议。
NAME_MEMORY_LIMIT = 200


# ==========================================
# 0.5 共用配置加载
# ==========================================
def load_config():
    if not os.path.exists(CONFIG_FILE):
        print("=" * 55)
        print("[!] 未找到 config.json 配置文件！")
        print("    请先运行安装向导完成初始化配置：")
        print()
        print("      python setup.py")
        print()
        print("=" * 55)
        sys.exit(1)
    with open(CONFIG_FILE, "r", encoding="utf-8") as f:
        return json.load(f)


# ==========================================
# 0.6 统一数据库初始化（build.py 和 main.py 共用）
# ==========================================
def _table_names(conn):
    return {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}


def _column_names(conn, table):
    try:
        return {row[1] for row in conn.execute(f'PRAGMA table_info("{table}")')}
    except sqlite3.Error:
        return set()


def _migrate_schema(conn):
    """对既有数据库做幂等的增量迁移，保证老库无需重建即可升级。"""
    # labs.source_id：记录该变种由哪台母体裂变而来。
    # 跳过已编译母体时以它为主判据 —— build_history 在历史库中可能为空。
    if "labs" in _table_names(conn) and "source_id" not in _column_names(conn, "labs"):
        conn.execute("ALTER TABLE labs ADD COLUMN source_id TEXT")


def ensure_db(db_file=DB_FILE):
    """确保所有表都已创建，并补齐索引与增量迁移。"""
    with _DB_WRITE_LOCK:
        conn = sqlite3.connect(db_file, timeout=30)
        try:
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
            cursor.execute('''CREATE TABLE IF NOT EXISTS bookmarks (
                id INTEGER PRIMARY KEY AUTOINCREMENT, operator_name TEXT,
                lab_id TEXT, question_text TEXT, question_focus TEXT,
                missed_insights TEXT, feedback TEXT, score INTEGER,
                timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
            )''')
            cursor.execute('''CREATE TABLE IF NOT EXISTS sm2_schedule (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                operator_name TEXT NOT NULL,
                lab_id TEXT NOT NULL,
                question_idx INTEGER NOT NULL,
                question_text TEXT,
                easiness REAL DEFAULT 2.5,
                interval INTEGER DEFAULT 1,
                repetitions INTEGER DEFAULT 0,
                next_review DATE DEFAULT (date('now')),
                last_score INTEGER DEFAULT 0,
                UNIQUE(operator_name, lab_id, question_idx)
            )''')
            cursor.execute('''CREATE TABLE IF NOT EXISTS build_history (
                original_name TEXT PRIMARY KEY, new_name TEXT
            )''')
            # 隔离用：记录某个代号是否设置了访问密码。
            # pass_hash 为空表示该代号零门槛（完全向后兼容）。
            cursor.execute('''CREATE TABLE IF NOT EXISTS operators (
                name TEXT PRIMARY KEY,
                pass_hash TEXT,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP
            )''')

            _migrate_schema(conn)
            for stmt in (
                "CREATE INDEX IF NOT EXISTS idx_submissions_operator ON submissions(operator_name)",
                "CREATE INDEX IF NOT EXISTS idx_submissions_lab ON submissions(lab_id)",
                "CREATE INDEX IF NOT EXISTS idx_bookmarks_operator ON bookmarks(operator_name)",
                "CREATE INDEX IF NOT EXISTS idx_sm2_operator_due ON sm2_schedule(operator_name, next_review)",
                "CREATE INDEX IF NOT EXISTS idx_labs_source ON labs(source_id)",
            ):
                cursor.execute(stmt)

            conn.commit()
        finally:
            conn.close()


# ==========================================
# 1. 变异方向指令池 (Mutation Angles)
# ==========================================
MUTATION_ANGLES = [
    "【隐蔽变异】：保留原笔记的完整攻击逻辑链，但彻底改变具体的应用名称、端口号、脚本语言和文件绝对路径。让它看起来像一台完全不同的机器。",
    "【入口变异】：改变初始立足点 (Initial Access) 的获取方式（例如将原笔记的 SQL 注入改为文件包含，或将弱口令改为反序列化），但严格保留原笔记的提权和后渗透逻辑。",
    "【提权变异】：保持原笔记的情报搜集和初始访问方式不变，但彻底改变提权 (Privilege Escalation) 的漏洞类型和利用手法。",
    "【深渊变异】：在情报搜集 (Context) 阶段，注入一个极具迷惑性的『兔子洞 (Rabbit Hole)』服务日志（如扫出了一个看起来有大洞的端口，但实际上无法利用）。将原笔记真正的突破口伪装得更加隐蔽。",
    "【阵营反转】：如果原笔记是 Windows，请将其合理转换并重构为 Linux 靶机环境（反之亦然），但必须巧妙地保留原笔记的核心渗透思维（如：将 Windows 的 SMB 凭证泄露转换为 Linux 的 NFS 共享泄露）。",
]


# ==========================================
# 2. Markdown 解析
# ==========================================
def parse_markdown_to_machines(filepath):
    """从 .md 文件中按 ## 标题提取多台靶机"""
    with open(filepath, "r", encoding="utf-8", errors="replace") as f:
        text = f.read()
    return parse_markdown_text_to_machines(text)


def parse_markdown_text_to_machines(text):
    """从 Markdown 文本中按 ## 标题提取多台靶机（支持直接传入文本）"""
    sections = re.split(r'^#{2}\s+(.+)$', text, flags=re.MULTILINE)
    machines = {}
    for i in range(1, len(sections), 2):
        name = sections[i].strip()
        content = sections[i + 1].strip()
        if name and content:
            original_id = f"HTB-{name}" if not name.startswith("HTB") else name
            machines[original_id] = content
    return machines


def smart_truncate(text, max_chars=7000):
    """按段落边界截断文本，避免在段落中间截断"""
    if len(text) <= max_chars:
        return text
    paragraphs = text.split('\n\n')
    result = []
    total = 0
    for para in paragraphs:
        if total + len(para) + 2 > max_chars:
            break
        result.append(para)
        total += len(para) + 2
    if not result:
        return text[:max_chars]
    return '\n\n'.join(result)


# ==========================================
# 3. 校验与归一（成品率保障）
# ==========================================
def normalize_domain(raw):
    """
    把模型给出的 domain 归一到白名单内。
    1) 精确匹配（忽略大小写与首尾空白）直接采用规范写法；
    2) 否则按关键词回退；
    3) 仍无法判定则降级为 Mixed。
    返回 (规范值, 是否发生了改写)
    """
    if not isinstance(raw, str) or not raw.strip():
        return "Mixed", True
    candidate = raw.strip()
    for allowed in ALLOWED_DOMAINS:
        if candidate.lower() == allowed.lower():
            return allowed, False
    lowered = candidate.lower()
    for keyword, mapped in _DOMAIN_KEYWORD_FALLBACK:
        if keyword in lowered:
            return mapped, True
    return "Mixed", True


def normalize_difficulty(raw):
    """把 difficulty 归一到三档之一。"""
    if isinstance(raw, str):
        candidate = raw.strip().capitalize()
        if candidate in ALLOWED_DIFFICULTIES:
            return candidate, False
    return "Medium", True


def normalize_lab_data(data):
    """
    对 LLM 返回的靶机数据做入库前的归一与结构校验。
    返回 (归一后的 data, warnings: list[str])

    Prompt 中的约定模型不保证遵守，因此必须有这一层确定性校验。
    """
    warnings = []

    if not isinstance(data, dict):
        return {}, ["返回结构不是 JSON 对象"]
    domain, changed = normalize_domain(data.get("domain"))
    if changed:
        warnings.append(f"domain 「{data.get('domain')}」不在白名单内，已归一为 「{domain}」")
    data["domain"] = domain
    difficulty, changed = normalize_difficulty(data.get("difficulty"))
    if changed:
        warnings.append(f"difficulty 「{data.get('difficulty')}」非法，已归一为 「{difficulty}」")
    data["difficulty"] = difficulty
    os_val = data.get("os")
    if not isinstance(os_val, str) or not os_val.strip():
        data["os"] = "Unknown"
        warnings.append("os 字段缺失，已置为 Unknown")
    else:
        data["os"] = os_val.strip()
    tags = data.get("tags")
    if isinstance(tags, str):
        tags = [t.strip() for t in re.split(r'[,，]', tags) if t.strip()]
    if not isinstance(tags, list):
        tags = []
        warnings.append("tags 不是数组，已置为空数组")
    data["tags"] = [str(t).strip() for t in tags if str(t).strip()][:12]

    # context 必须是纯英文终端日志，且体量合理
    context = data.get("context")
    if not isinstance(context, str) or len(context.strip()) < 200:
        warnings.append("context 过短或缺失，疑似生成失败")
        data["context"] = context if isinstance(context, str) else ""
    else:
        if re.search(r'[\u4e00-\u9fff]', context):
            warnings.append("context 中混入了中文（要求为纯英文终端日志）")
        # 尾部残留的截断提示语属于泄题红线
        if re.search(r'(\[?\s*(断头台|截断|省略|此处省略)\s*\]?|REDACTED)', context[-300:], re.I):
            warnings.append("context 尾部残留截断/打码提示语，违反无痕截断要求")
        data["context"] = context
    questions = data.get("questions")
    if not isinstance(questions, list):
        questions = []
        warnings.append("questions 不是数组，已置为空数组")
    cleaned_q = []
    for q in questions:
        if isinstance(q, dict) and isinstance(q.get("text"), str) and q["text"].strip():
            cleaned_q.append({
                "text": q["text"].strip(),
                "focus": (q.get("focus") or "").strip() if isinstance(q.get("focus"), str) else "",
            })
    data["questions"] = cleaned_q
    if len(cleaned_q) < 3:
        warnings.append(f"题目数量 {len(cleaned_q)} 少于要求的 3 道")
    for q in cleaned_q:
        if re.match(r'^\s*(任务|问题|Question)\s*0?\d+\s*[:：.、]', q["text"]):
            warnings.append("题目正文带有编号前缀，会与前端自动编号重复")
            break
    focus = data.get("focus_points")
    if isinstance(focus, str) and focus.strip():
        data["focus_points"] = focus.strip()
    elif isinstance(focus, list):
        data["focus_points"] = "\n".join(
            f"{i}. {str(x).strip()}" for i, x in enumerate(focus, 1) if str(x).strip())
    else:
        data["focus_points"] = ""
        warnings.append("focus_points 缺失")

    # questions 与 focus_points 应 1:1 对应（Prompt 明确要求）
    pts = len(re.findall(r'^\s*\d+[\.、]', data["focus_points"], flags=re.MULTILINE))
    if pts and cleaned_q and pts != len(cleaned_q):
        warnings.append(f"题目数({len(cleaned_q)}) 与考点数({pts}) 不满足 1:1")
    name = data.get("machine_name")
    if not isinstance(name, str) or not name.strip():
        data["machine_name"] = ""
        warnings.append("machine_name 缺失")
    else:
        data["machine_name"] = name.strip()

    return data, warnings


# ==========================================
# 4. 数据库操作
# ==========================================
def get_db(db_file=DB_FILE):
    conn = sqlite3.connect(db_file, timeout=30)
    conn.row_factory = sqlite3.Row
    return conn


def get_all_used_machine_names(db_file=DB_FILE):
    """获取数据库中所有已有靶机名"""
    try:
        conn = get_db(db_file)
        try:
            return [row[0] for row in conn.execute("SELECT id FROM labs")]
        finally:
            conn.close()
    except sqlite3.Error:
        return []


def get_difficulty_distribution(db_file=DB_FILE):
    """统计当前库内的难度分布，用于在 Prompt 中提示模型补齐缺失档位。"""
    try:
        conn = get_db(db_file)
        try:
            rows = conn.execute(
                "SELECT difficulty, COUNT(*) FROM labs GROUP BY difficulty").fetchall()
            dist = {d: 0 for d in ALLOWED_DIFFICULTIES}
            for d, n in rows:
                if d in dist:
                    dist[d] = n
            return dist
        finally:
            conn.close()
    except sqlite3.Error:
        return {d: 0 for d in ALLOWED_DIFFICULTIES}


def save_lab_to_db(history_id, data, db_file=DB_FILE, source_id=None):
    """
    将生成的靶机写入数据库。

    注意：本函数会被批量编译的多线程并发调用，因此内部串行化。
    它不得被包进「调用 LLM 的重试块」里——写库失败只应重试写库，
    绝不能因此重新调用一次计费模型。
    """
    new_machine_name = data.get('machine_name', history_id)
    with _DB_WRITE_LOCK:
        conn = sqlite3.connect(db_file, timeout=30)
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            cursor = conn.cursor()
            cursor.execute(
                '''INSERT OR REPLACE INTO labs
                   (id, os, difficulty, domain, tags, context, questions, focus_points, source_id)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)''',
                (
                    new_machine_name,
                    data.get('os', 'Unknown'),
                    data.get('difficulty', 'Medium'),
                    data.get('domain', 'General'),
                    json.dumps(data.get('tags', []), ensure_ascii=False),
                    data['context'],
                    json.dumps(data['questions'], ensure_ascii=False),
                    data['focus_points'],
                    source_id,
                )
            )
            cursor.execute(
                '''INSERT OR REPLACE INTO build_history (original_name, new_name)
                   VALUES (?, ?)''',
                (history_id, new_machine_name)
            )
            conn.commit()
        finally:
            conn.close()
    return new_machine_name


def deduplicate_name(name, used_names):
    """防止名称碰撞：如果名称已存在，加后缀；全部碰撞则 UUID 兜底"""
    original = name
    suffixes = ['Prime', 'Nexus', 'Apex', 'Echo', 'Forge', 'Nova', 'Vanguard', 'Shade', 'Fury', 'Ghost']
    for suffix in suffixes:
        if name not in used_names:
            return name
        base = original.split('-')[0]
        name = f"{base}-{suffix}"
    if name in used_names:
        base = original.split('-')[0]
        name = f"{base}-{uuid.uuid4().hex[:6].upper()}"
    return name


def reserve_machine_name(proposed, used_names, db_file=DB_FILE):
    """
    原子地为一个变种占下一个唯一名字。

    原实现是 deduplicate_name(检查) 与 used_names.append(占用) 两步分离，
    且在 quality_check 开启时会隔着一次秒级 LLM 调用，并发下两个线程
    可同时通过检查，随后 INSERT OR REPLACE 让后者静默覆盖前者。
    这里把「检查 + 占用」合并进同一把锁。
    """
    with _NAME_LOCK:
        # 以数据库为准重新同步一次，避免多次运行之间失配
        pool = set(used_names) | set(get_all_used_machine_names(db_file))
        name = deduplicate_name(proposed or "Phantom", pool)
        used_names.append(name)
        return name


def release_machine_name(name, used_names):
    """写库失败时回滚已占用的名字，避免名字被虚占。"""
    with _NAME_LOCK:
        while name in used_names:
            used_names.remove(name)


# ==========================================
# 5. 构建 LLM Prompt
# ==========================================
def build_mutation_prompt(wp_text, mutation_angle, used_names_list, difficulty_hint=None):
    """构建靶机生成的 system prompt"""
    # 只喂基名：需避免的是基名重复，比喂全量变体名更省 token。
    base_names = []
    for n in (used_names_list or [])[-NAME_MEMORY_LIMIT:]:
        base = str(n).split('-')[0]
        if base and base not in base_names:
            base_names.append(base)
    used_names_str = ", ".join(base_names) if base_names else "无"

    difficulty_block = ""
    if difficulty_hint:
        total = sum(difficulty_hint.values()) or 0
        dist_str = " / ".join(f"{d} {difficulty_hint.get(d, 0)}" for d in ALLOWED_DIFFICULTIES)
        difficulty_block = (
            f"\n    # 📊 当前题库难度分布（用于保持三档均衡）\n"
            f"    现有 {total} 台，分布为：{dist_str}。\n"
            f"    请优先考虑把本台评定为当前**数量最少**的那一档，以维持 Easy / Medium / Hard "
            f"的均衡；但若情报本身明显偏向某一档，仍应诚实评定，不要为了配额而虚报。\n"
        )

    system_prompt = f"""
    # Role
    你是顶级红队靶场架构师与终端模拟器。你将收到一份真实的 OSCP 通关笔记作为"母体基因"。
    任务是：吸收母体笔记中真实、精妙的逻辑链，并执行【变异衍生 (Mutational Fission)】，创造一台全新的靶机。

    # 🧬 强制变异指令 (CRITICAL MUTATION REQUIREMENT)
    你必须严格基于以下变异策略对母体基因进行重构：
    >>> {mutation_angle} <<<
    ⚠️ 必须完全抛弃原靶机的名字、IP、域名。随机生成新的 IP 和环境信息。

    # 💎 极客命名死锁法则 (CRITICAL NAMING RULE)
    1. 必须基于变异后的核心漏洞起一个【极客感十足、隐喻性强的单词/双词代号】（风格参考 HackTheBox，如：Phantom, Goliath, Mirage, Bloodline）。绝对禁止使用 "Corp-Server-01" 这种枯燥的编号！
    2. ⚠️ 绝对禁止在名字中包含任何版本号、数字或下划线（严禁出现 -v2, _v1, 01 等字眼）！
    3. ⚠️ 记忆黑名单（以下**基名**已被占用，禁止再次使用）：[{used_names_str}]。
       注意：系统会在你之后自动追加去重后缀，所以**请务必提出一个全新的基名**，
       而不是依赖系统帮你改名。全新基名是硬性要求。

    # Requirements (严苛的纸上演练逻辑 - 黄金准则)
    1. 身份识别：识别变异后新靶机的 OS、难度、技术标签，以及所属的领域 (Domain)。
       ⚠️ Domain 死锁：domain 字段必须且只能从以下固定列表中选择一个，禁止自造新名称：
       [ "Web Application", "Active Directory", "Network Services", "Linux Privilege Escalation", "Windows Privilege Escalation", "Internal Network", "Mixed" ]
       ⚠️ Difficulty 死锁：difficulty 字段必须且只能填写 "Easy"、"Medium"、"Hard" 三者之一。请根据变异后的靶机复杂度诚实判断，三档都应该被使用到，不要全部填 Medium。
       {difficulty_block}
    2. 📜 绝对原始回显伪造 (对抗大白话与脏字符清洗)：
       - 致命错误：用一句中文大白话总结扫描结果！绝对禁止！
       - 必须为新靶机亲手**伪造出原汁原味的纯英文终端格式日志**（如 Nmap, Gobuster, smbclient 等）。
       - 乱码或十六进制符请替换为 `[HEX_DATA]`。
       - ⚠️ 凭证伪造指令：前期日志中如果出现敏感凭据（如明文密码、NTLM Hash、SSH 私钥等），绝对禁止打码或使用 [REDACTED]！你必须亲手伪造出极其逼真的假数据（如 admin:Winter2024!），让推演者看到真实的情报流。

    3. 🚨 断头台无痕截断 (Silent Guillotine - 绝对禁止剧透与提示语)：
       - 🔪 斩断：情报 (context) 只能包含变异后的前期扫描、枚举。一旦进入"成功获取初始立足点"、"执行漏洞利用"或"提权"，立刻停止提取，斩断后续所有内容！
       - ⚠️ 致命红线：截断必须**无痕**！绝对不允许在日志末尾输出"[断头台截断...]"、"[此处省略]"等任何提示语！要让日志看起来是自然结束的。

    4. 🚫 严禁画蛇添足 (防多余总结)：
       - Context 必须在终端代码（如 smbclient 下载提示或 Nmap 结果）输出完毕后**直接闭包结束**！绝对不允许在 Context 末尾生成类似 "INITIAL FOOTPRINT ANALYSIS" 或任何总结性质的大白话段落！

    5. ⛓️ 逻辑强绑定原则 (闭环防幻觉)：
       - Questions 必须【绝对严格地】与伪造的 Context 日志内容严丝合缝！
       - 绝不能在题目中硬编码"基于 ## 02 段落"这种死板字眼，直接描述线索即可。
       - 确保 questions 的数量与 focus_points 中总结的要点数量 1:1 绝对相等！不能出现"考点里有，但题目没问"的情况。

    6. 任务本质与引导：
       - 基于受限情报的推演。严禁使用"去破解这个密码"等动作指令。提示学生去观察特定细节。

    # 🌍 语言与数量死锁 (Language & Quantity Lock - 致命红线)
    - `context` 字段必须是【纯英文】的机器日志，毫无任何系统提示词。
    - ⚠️ `questions` 和 `focus_points` **必须强制使用纯中文输出！** 绝对不允许在题目中飙英文！
    - ⚠️ 数组中**必须包含至少 3 个任务**（视具体情报而定，3题、4题、5题皆可，但不能少于3题）。
    - ⚠️ **严禁在 text 开头写"任务01："等编号字眼，必须直接写出问题本身！前端系统会自动排版加编号。**
    - ⚠️ 最后一个问题必须固定为开放性问题，询问成功获取立足点后的【后续渗透思路或提权推演】。
    - ⚠️ JSON转义致命警告：在伪造 context 字段的终端日志时，如果包含双引号 (")、反斜杠 (\\，如 Windows 路径或正则)、换行符等特殊字符，【必须】严格遵循 JSON 规范进行转义（如写成 \\", \\\\, \\n）。绝不允许输出破坏 JSON 结构的未闭合字符串！

    # JSON Output Structure (严格模仿此格式的结尾和提问方式)
    {{
        "machine_name": "Phantom",
        "os": "Windows",
        "difficulty": "Medium",
        "domain": "Active Directory",
        "tags": ["SMB", "Information Leak"],
        "context": "## 01. NETWORK RECONNAISSANCE\\nStarting Nmap 7.92...\\nNmap scan report for 10.10.11.23\\n(全英文逼真伪造日志)\\n\\n## 02. SERVICE ENUMERATION\\nsmb: \\\\> get pass.txt\\ngetting file \\\\pass.txt\\nAdminBackup: Fall2024!@#",
        "questions": [
            {{ "text": "基于 SMB 获取到的 pass.txt，下一步该如何利用此凭证？", "focus": "考察凭证重用与横向移动。" }},
            {{ "text": "在 XXX 服务中发现的特征...，可能存在哪种注入风险？", "focus": "考察对未知接口的测试思路。" }},
            {{ "text": "结合目前掌握的所有情报，请简述成功获取初始立足点后的后续渗透或提权推演思路？", "focus": "考察系统权限提升和后渗透大局观。" }}
        ],
        "focus_points": "1. 预期第一步利用链：使用密码尝试登录...\\n2. 预期第二步利用链：利用接口漏洞...\\n3. 预期的后续提权思路：获取低权限后寻找内核漏洞..."
    }}
    """
    return system_prompt


def build_user_prompt(wp_text):
    """构建靶机生成的 user prompt"""
    truncated = smart_truncate(wp_text, max_chars=7000)
    return (f"请提取考点并基于以下母体笔记进行变异衍生。强制：伪造英文终端日志"
            f"(结尾绝对不写总结/不留提示语)、中文提问(无编号前缀)、至少3题、"
            f"起一个全新的极客基名、domain/difficulty 只能取指定白名单值。严格无痕截断！"
            f"WP 内容：\n\n{truncated}")


def clean_and_parse_json(raw_json_str):
    """清洗 LLM 返回的 JSON 字符串中的脏字符，然后解析"""
    clean = re.sub(r'[\x00-\x08\x0b\x0c\x0e-\x1f]', '', raw_json_str)
    return json.loads(clean, strict=False)


# ==========================================
# 6. 质量评分过滤
# ==========================================
QUALITY_MIN_SCORE = 6  # 低于此分的靶机不入库


def build_quality_check_prompt():
    """构建质量评分的 system prompt"""
    return """你是 OSCP 靶场质量审核官。请对以下生成的靶机 JSON 进行质量打分（1-10分），维度：
1. context 是否为纯英文逼真终端日志（非大白话总结）
2. questions 是否为纯中文（无英文混入）且 ≥3 题
3. questions 数量是否与 focus_points 要点数 1:1 对应
4. context 是否在情报搜集阶段自然截断（无剧透、无提示语）
5. machine_name 是否为极客代号（无数字编号）

只输出 JSON：{"quality_score": 8, "issues": ["问题1", "问题2"]}
如果全部合格则 issues 为空数组。"""


def quality_check(client, model, lab_data):
    """对生成的靶机进行质量评分，返回 (score, issues)"""
    try:
        kwargs = {
            "model": model,
            "messages": [
                {"role": "system", "content": build_quality_check_prompt()},
                {"role": "user", "content": json.dumps(lab_data, ensure_ascii=False)[:4000]}
            ],
            "temperature": 0.1,
            "max_tokens": 500,
        }
        kwargs.update(_json_mode_arg())
        try:
            resp = client.chat.completions.create(**kwargs)
        except Exception as e:
            # 端点在本次运行中首次暴露「不支持 response_format」时降级重试一次
            if _response_format_rejected(e) and _disable_json_mode():
                kwargs.pop("response_format", None)
                resp = client.chat.completions.create(**kwargs)
            else:
                raise
        result = clean_and_parse_json(resp.choices[0].message.content)
        return result.get("quality_score", 0), result.get("issues", [])
    except Exception as e:
        print(f"   ⚠️ 质量评分失败，跳过过滤: {e}")
        return 10, []  # 评分失败时放行


# ==========================================
# 7. 单变种生成（核心）
# ==========================================
# 服务商下线或改名模型时，报错文案各不相同
# （DeepSeek 旧版为 "Model Not Exist"，OpenAI 为 "The model `x` does not exist"），
# 统一翻译成可操作的指引，避免用户对着 400 发懵。
_MODEL_GONE_HINTS = (
    "model not exist", "model_not_found", "model not found",
    "does not exist", "unknown model", "invalid model",
    "no such model", "unsupported model", "not a valid model",
)


# 并非所有 OpenAI 兼容端点都接受 response_format={"type":"json_object"}：
# 部分服务商/兼容层会以 400 直接拒绝（如 Anthropic 的兼容层报
# "response_format: Extra inputs are not permitted"），另有部分会静默忽略。
# 首次遇到「被拒绝」时自动降级为不携带该参数重试，并在此后不再携带 ——
# JSON 结构仍由 Prompt 约束 + clean_and_parse_json 兜底。
# 被拒绝的请求返回 400、不消耗 token，因此这个降级不产生额外费用。
_JSON_MODE_SUPPORTED = True


def _response_format_rejected(err):
    low = str(err).lower()
    return "response_format" in low or "json_object" in low


def _disable_json_mode():
    """
    关闭 JSON 模式，返回「本次调用是否真的发生了状态变化」（幂等）。

    返回值用于调用方判断是否值得再试一次 —— 也避免因错误文案重复命中
    而陷入无限重试。
    """
    global _JSON_MODE_SUPPORTED
    changed = _JSON_MODE_SUPPORTED
    _JSON_MODE_SUPPORTED = False
    return changed


def _json_mode_arg():
    """需要时返回 response_format 参数字典，否则返回空字典。"""
    return {"response_format": {"type": "json_object"}} if _JSON_MODE_SUPPORTED else {}


def _explain_llm_error(msg):
    low = str(msg).lower()
    if any(h in low for h in _MODEL_GONE_HINTS):
        return (f"{msg}\n     → 配置的模型名可能已失效（服务商已下线或改名）。"
                f"请重新运行 python setup.py，从当前可用模型列表中选择。")
    return msg


def _generate_raw_variant(client, model, wp_text, original_id, variant_idx,
                          used_names, difficulty_hint):
    """
    阶段一：调用 LLM 并解析出结构化数据。

    只重试「模型调用 + JSON 解析」——这两步计费，但重试它们本身有意义
    （针对模型偶发的格式错误）。任何数据库/文件系统操作都不得出现在本函数内。
    """
    current_mutation = random.choice(MUTATION_ANGLES)
    system_prompt = build_mutation_prompt(wp_text, current_mutation, used_names, difficulty_hint)
    user_prompt = build_user_prompt(wp_text)

    last_error = "未知错误"
    attempts = 0
    while attempts < 3:
        kwargs = {
            "model": model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt}
            ],
            "temperature": 0.7,
            "max_tokens": 4000,
        }
        kwargs.update(_json_mode_arg())
        try:
            response = client.chat.completions.create(**kwargs)
            return clean_and_parse_json(response.choices[0].message.content), None
        except json.JSONDecodeError:
            attempts += 1
            last_error = "JSON 格式错误"
        except Exception as e:
            if _response_format_rejected(e) and _disable_json_mode():
                continue                  # 刚降级，用不带 response_format 的参数重试
            attempts += 1
            last_error = _explain_llm_error(f"生成异常: {str(e)}")
    return None, f"{last_error}（已重试 3 次）"


def generate_single_variant(client, model, wp_text, original_id, variant_idx,
                            global_used_names, db_file=DB_FILE, enable_quality_check=True):
    """
    生成单个变种靶机。供 build.py 和上传 API 共同调用。

    返回: (success: bool, machine_name: str | None, lab_data: dict | None, error: str | None)

    「LLM 阶段」与「持久化阶段」必须各自重试。
    若把 save_lab_to_db 并入 LLM 所在的 try 块，写库失败会连带重新调用一次
    计费模型（多线程写 SQLite 有概率失败），而第一份产出被丢弃。
    """
    variant_history_id = f"{original_id}_v{variant_idx}"

    # ---------- 阶段一：LLM 生成（计费，失败才重试） ----------
    difficulty_hint = get_difficulty_distribution(db_file)
    raw_data, err = _generate_raw_variant(
        client, model, wp_text, original_id, variant_idx,
        list(global_used_names), difficulty_hint
    )
    if raw_data is None:
        return False, None, None, err

    # ---------- 阶段二：归一与校验（本地，零成本） ----------
    lab_data, warnings = normalize_lab_data(raw_data)
    if not lab_data.get("context") or not lab_data.get("questions"):
        return False, None, lab_data, "归一后缺少 context 或 questions，判为无效产出"
    for w in warnings:
        print(f"   ℹ️ [{original_id} v{variant_idx}] 归一提示: {w}")

    # ---------- 阶段三：质量评分（可选，计费） ----------
    if enable_quality_check:
        score, issues = quality_check(client, model, lab_data)
        if score < QUALITY_MIN_SCORE:
            return False, None, lab_data, f"质量评分 {score}/10 未达标: {', '.join(issues)}"

    # ---------- 阶段四：原子占名（本地） ----------
    proposed = lab_data.get("machine_name") or "Phantom"
    new_name = reserve_machine_name(proposed, global_used_names, db_file)
    lab_data["machine_name"] = new_name

    # ---------- 阶段五：写库（本地；失败只重试写库，绝不重调模型） ----------
    last_db_error = None
    for w_attempt in range(3):
        try:
            saved = save_lab_to_db(variant_history_id, lab_data, db_file, source_id=original_id)
            return True, saved, lab_data, None
        except sqlite3.Error as e:
            last_db_error = f"写库失败({type(e).__name__}): {e}"
            time.sleep(0.3 * (w_attempt + 1))
        except Exception as e:
            last_db_error = f"写库异常: {e}"
            break

    # 写库彻底失败：把名字还回去，避免虚占
    release_machine_name(new_name, global_used_names)
    return False, None, lab_data, last_db_error or "写库失败"
