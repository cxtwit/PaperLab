#!/usr/bin/env python3
# -*- coding: utf-8 -*-
#
# PaperLab — 共用的靶机 LLM 生成逻辑
# 供 build.py 批量编译和 main.py 上传裂变 API 共同调用

import json
import os
import re
import random
import sqlite3
import sys
import uuid

DB_FILE = "paperlab.db"
CONFIG_FILE = "config.json"


# ==========================================
# 0. 共用配置加载
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
# 0.5 统一数据库初始化（build.py 和 main.py 共用）
# ==========================================
def ensure_db(db_file=DB_FILE):
    """确保所有表都已创建。build.py 和 main.py 启动时都调用此函数。"""
    conn = sqlite3.connect(db_file)
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
    conn.commit()
    conn.close()

# ==========================================
# 1. 变异方向指令池 (Mutation Angles)
# ==========================================
MUTATION_ANGLES = [
    "【隐蔽变异】：保留原笔记的完整攻击逻辑链，但彻底改变具体的应用名称、端口号、脚本语言和文件绝对路径。让它看起来像一台完全不同的机器。",
    "【入口变异】：改变初始立足点 (Initial Access) 的获取方式（例如将原笔记的 SQL 注入改为文件包含，或将弱口令改为反序列化），但严格保留原笔记的提权和后渗透逻辑。",
    "【提权变异】：保持原笔记的情报搜集和初始访问方式不变，但彻底改变提权 (Privilege Escalation) 的漏洞类型和利用手法。",
    "【深渊变异】：在情报搜集 (Context) 阶段，注入一个极具迷惑性的『兔子洞 (Rabbit Hole)』服务日志（如扫出了一个看起来有大洞的端口，但实际上无法利用）。将原笔记真正的突破口伪装得更加隐蔽。",
    "【阵营反转】：如果原笔记是 Windows，请将其合理转换并重构为 Linux 靶机环境（反之亦然），但必须巧妙地保留原笔记的核心渗透思维（如：将 Windows 的 SMB 凭证泄露转换为 Linux 的 NFS 共享泄露）。"
]

# ==========================================
# 2. Markdown 解析
# ==========================================
def parse_markdown_to_machines(filepath):
    """从 .md 文件中按 ## 标题提取多台靶机"""
    with open(filepath, "r", encoding="utf-8") as f:
        text = f.read()
    return parse_markdown_text_to_machines(text)


def parse_markdown_text_to_machines(text):
    """从 Markdown 文本中按 ## 标题提取多台靶机（支持直接传入文本）"""
    sections = re.split(r'^#{2}\s+(.+)$', text, flags=re.MULTILINE)
    machines = {}
    for i in range(1, len(sections), 2):
        name = sections[i].strip()
        content = sections[i+1].strip()
        if name and content:
            original_id = f"HTB-{name}" if not name.startswith("HTB") else name
            machines[original_id] = content
    return machines


def smart_truncate(text, max_chars=7000):
    """按段落边界截断文本，避免在段落中间截断"""
    if len(text) <= max_chars:
        return text
    # 按双换行分段
    paragraphs = text.split('\n\n')
    result = []
    total = 0
    for para in paragraphs:
        if total + len(para) + 2 > max_chars:
            break
        result.append(para)
        total += len(para) + 2
    # 至少保留一段
    if not result:
        return text[:max_chars]
    return '\n\n'.join(result)


# ==========================================
# 3. 数据库操作
# ==========================================
def get_all_used_machine_names(db_file=DB_FILE):
    """获取数据库中所有已有靶机名"""
    try:
        conn = sqlite3.connect(db_file)
        cursor = conn.cursor()
        cursor.execute("SELECT id FROM labs")
        rows = cursor.fetchall()
        conn.close()
        return [row[0] for row in rows]
    except Exception:
        return []


def save_lab_to_db(history_id, data, db_file=DB_FILE):
    """将生成的靶机写入数据库"""
    new_machine_name = data.get('machine_name', history_id)
    conn = sqlite3.connect(db_file)
    cursor = conn.cursor()
    cursor.execute(
        '''INSERT OR REPLACE INTO labs (id, os, difficulty, domain, tags, context, questions, focus_points)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)''',
        (
            new_machine_name,
            data.get('os', 'Unknown'),
            data.get('difficulty', 'Medium'),
            data.get('domain', 'General'),
            json.dumps(data.get('tags', []), ensure_ascii=False),
            data['context'],
            json.dumps(data['questions'], ensure_ascii=False),
            data['focus_points']
        )
    )
    cursor.execute(
        '''INSERT OR REPLACE INTO build_history (original_name, new_name) VALUES (?, ?)''',
        (history_id, new_machine_name)
    )
    conn.commit()
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
    # 全部后缀碰撞，使用 UUID 短串兜底，保证不重复
    if name in used_names:
        base = original.split('-')[0]
        name = f"{base}-{uuid.uuid4().hex[:6].upper()}"
    return name


# ==========================================
# 4. 构建 LLM Prompt
# ==========================================
def build_mutation_prompt(wp_text, mutation_angle, used_names_list):
    """构建靶机生成的 system prompt"""
    used_names_str = ", ".join(used_names_list[-20:]) if used_names_list else "无"

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
    3. ⚠️ 记忆黑名单：为了防止重复，你本次起的名字绝对不能是以下已被占用的名字：[{used_names_str}]。必须想一个全新的！

    # Requirements (严苛的纸上演练逻辑 - 黄金准则)
    1. 身份识别：识别变异后新靶机的 OS、难度、技术标签，以及所属的领域 (Domain)。
       ⚠️ Domain 死锁：domain 字段必须且只能从以下固定列表中选择一个，禁止自造新名称：
       [ "Web Application", "Active Directory", "Network Services", "Linux Privilege Escalation", "Windows Privilege Escalation", "Internal Network", "Mixed" ]
       ⚠️ Difficulty 死锁：difficulty 字段必须且只能填写 "Easy"、"Medium"、"Hard" 三者之一。请根据变异后的靶机复杂度诚实判断，三档都应该被使用到，不要全部填 Medium。

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
    return f"请提取考点并基于以下母体笔记进行变异衍生。强制：伪造英文终端日志(结尾绝对不写总结/不留提示语)、中文提问(无编号前缀)、至少3题、起个极客名字。严格无痕截断！WP 内容：\n\n{truncated}"


def clean_and_parse_json(raw_json_str):
    """清洗 LLM 返回的 JSON 字符串中的脏字符，然后解析"""
    clean = re.sub(r'[\x00-\x08\x0b\x0c\x0e-\x1f]', '', raw_json_str)
    return json.loads(clean, strict=False)


# ==========================================
# 5. 质量评分过滤
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
        resp = client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": build_quality_check_prompt()},
                {"role": "user", "content": json.dumps(lab_data, ensure_ascii=False)[:4000]}
            ],
            temperature=0.1,
            max_tokens=500,
            response_format={"type": "json_object"}
        )
        result = clean_and_parse_json(resp.choices[0].message.content)
        return result.get("quality_score", 0), result.get("issues", [])
    except Exception as e:
        print(f"   ⚠️ 质量评分失败，跳过过滤: {e}")
        return 10, []  # 评分失败时放行


def generate_single_variant(client, model, wp_text, original_id, variant_idx, global_used_names, db_file=DB_FILE, enable_quality_check=True):
    """
    生成单个变种靶机。供 build.py 和上传 API 共同调用。

    返回: (success: bool, machine_name: str | None, lab_data: dict | None, error: str | None)
    """
    current_mutation = random.choice(MUTATION_ANGLES)
    variant_history_id = f"{original_id}_v{variant_idx}"

    used_names_str_list = list(global_used_names[-20:]) if global_used_names else []

    system_prompt = build_mutation_prompt(wp_text, current_mutation, used_names_str_list)
    user_prompt = build_user_prompt(wp_text)

    max_retries = 3
    for attempt in range(max_retries):
        try:
            response = client.chat.completions.create(
                model=model,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt}
                ],
                temperature=0.7,
                max_tokens=4000,
                response_format={"type": "json_object"}
            )

            lab_data = clean_and_parse_json(response.choices[0].message.content)

            # 防名称碰撞
            ai_name = lab_data.get('machine_name', 'Phantom')
            ai_name = deduplicate_name(ai_name, global_used_names)
            lab_data['machine_name'] = ai_name

            # 质量评分过滤
            if enable_quality_check:
                score, issues = quality_check(client, model, lab_data)
                if score < QUALITY_MIN_SCORE:
                    return False, None, lab_data, f"质量评分 {score}/10 未达标: {', '.join(issues)}"

            # 写入数据库
            global_used_names.append(ai_name)
            new_name = save_lab_to_db(variant_history_id, lab_data, db_file)

            return True, new_name, lab_data, None

        except json.JSONDecodeError:
            if attempt == max_retries - 1:
                return False, None, None, f"JSON 格式错误（已重试 {max_retries} 次）"
        except Exception as e:
            if attempt == max_retries - 1:
                return False, None, None, f"生成异常: {str(e)}"

    return False, None, None, "未知错误"
