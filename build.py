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
import re
import sqlite3
import random
from openai import OpenAI

# ==========================================
# 1. 核心配置区
# ==========================================
client = OpenAI(
    api_key="YOUR_API_KEY_HERE",
    base_url="https://api.deepseek.com/v1",
    timeout=120.0  # 护猫盾：增加超时时间，防止 AI 思考过久断连
)

MD_DIR = "md"
DB_FILE = "paperlab.db"
TEST_MODE_LIMIT = 50 

# 💡 衍生倍率：一份真实的 MD 笔记，裂变出几个不同的变种靶机？
DERIVE_COUNT = 3  

# 💡 留空则全量编译 md 目录下所有未编译的笔记；写上名字（加引号）则只编译指定的笔记
TARGET_LABS = [] 

# ==========================================
# 2. 变异方向指令池 (Mutation Angles)
# ==========================================
MUTATION_ANGLES = [
    "【隐蔽变异】：保留原笔记的完整攻击逻辑链，但彻底改变具体的应用名称、端口号、脚本语言和文件绝对路径。让它看起来像一台完全不同的机器。",
    "【入口变异】：改变初始立足点 (Initial Access) 的获取方式（例如将原笔记的 SQL 注入改为文件包含，或将弱口令改为反序列化），但严格保留原笔记的提权和后渗透逻辑。",
    "【提权变异】：保持原笔记的情报搜集和初始访问方式不变，但彻底改变提权 (Privilege Escalation) 的漏洞类型和利用手法。",
    "【深渊变异】：在情报搜集 (Context) 阶段，注入一个极具迷惑性的『兔子洞 (Rabbit Hole)』服务日志（如扫出了一个看起来有大洞的端口，但实际上无法利用）。将原笔记真正的突破口伪装得更加隐蔽。",
    "【阵营反转】：如果原笔记是 Windows，请将其合理转换并重构为 Linux 靶机环境（反之亦然），但必须巧妙地保留原笔记的核心渗透思维（如：将 Windows 的 SMB 凭证泄露转换为 Linux 的 NFS 共享泄露）。"
]

# ==========================================
# 3. 数据库底层支持
# ==========================================
def auto_init_db():
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute('''CREATE TABLE IF NOT EXISTS labs (id TEXT PRIMARY KEY, os TEXT, difficulty TEXT, domain TEXT, tags TEXT, context TEXT, questions TEXT, focus_points TEXT)''')
    cursor.execute('''CREATE TABLE IF NOT EXISTS submissions (id INTEGER PRIMARY KEY AUTOINCREMENT, lab_id TEXT, operator_name TEXT, student_writeup TEXT, report TEXT, timestamp DATETIME DEFAULT CURRENT_TIMESTAMP)''')
    cursor.execute('''CREATE TABLE IF NOT EXISTS build_history (original_name TEXT PRIMARY KEY, new_name TEXT)''')
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
    except:
        return []

# 🛡️ 拉取数据库里【所有的】靶机名字，防止跨文件全局互相覆盖
def get_all_used_machine_names():
    try:
        conn = sqlite3.connect(DB_FILE)
        cursor = conn.cursor()
        cursor.execute("SELECT id FROM labs")
        rows = cursor.fetchall()
        conn.close()
        return [row[0] for row in rows]
    except:
        return []

def save_to_db(history_id, data):
    new_machine_name = data.get('machine_name', history_id) 
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute('''INSERT OR REPLACE INTO labs (id, os, difficulty, domain, tags, context, questions, focus_points) VALUES (?, ?, ?, ?, ?, ?, ?, ?)''', (
        new_machine_name, data.get('os', 'Unknown'), data.get('difficulty', 'Medium'), data.get('domain', 'General'), json.dumps(data.get('tags', []), ensure_ascii=False), data['context'], json.dumps(data['questions'], ensure_ascii=False), data['focus_points']
    ))
    cursor.execute('''INSERT OR REPLACE INTO build_history (original_name, new_name) VALUES (?, ?)''', (history_id, new_machine_name))
    conn.commit()
    conn.close()
    return new_machine_name

def parse_markdown_to_machines(filepath):
    with open(filepath, "r", encoding="utf-8") as f:
        text = f.read()
    sections = re.split(r'^#{2}\s+(.+)$', text, flags=re.MULTILINE)
    machines = {}
    for i in range(1, len(sections), 2):
        name = sections[i].strip()
        content = sections[i+1].strip()
        if name and content:
            original_id = f"HTB-{name}" if not name.startswith("HTB") else name
            machines[original_id] = content
    return machines

# ==========================================
# 4. 变异裂变核心逻辑
# ==========================================
def build_pro_database():
    auto_init_db()
    existing_labs = get_existing_labs()
    global_used_names = get_all_used_machine_names()
    
    if not os.path.exists(MD_DIR):
        print(f"⚠️ 目录 {MD_DIR} 不存在，请创建并在其中放入 .md 笔记文件。")
        return

    #PREVIEW_DIR = "preview_json"
    #os.makedirs(PREVIEW_DIR, exist_ok=True)

    compiled_count = 0
    for filename in os.listdir(MD_DIR):
        if not filename.endswith(".md"): continue
        filepath = os.path.join(MD_DIR, filename)
        
        machines_dict = parse_markdown_to_machines(filepath)
        
        for original_id, wp_text in machines_dict.items():
            if TARGET_LABS and original_id not in TARGET_LABS:
                continue 
            
            # 如果变种 0 已经存在，说明这个母体已经被处理过了
            base_history_check = f"{original_id}_v0"
            if not TARGET_LABS and base_history_check in existing_labs:
                continue 

            if compiled_count >= TEST_MODE_LIMIT:
                return

            print(f"\n==================================================")
            print(f"⚙️  提取母体基因 [{original_id}]，准备执行 {DERIVE_COUNT} 次变异衍生...")
            
            for variant_idx in range(DERIVE_COUNT):
                current_mutation = random.choice(MUTATION_ANGLES)
                variant_history_id = f"{original_id}_v{variant_idx}"
                
                # 防止 Prompt 太长，只喂给 AI 最近的 20 个名字让它避开
                used_names_str = ", ".join(global_used_names[-20:]) if global_used_names else "无"
                
                print(f"\n   🔬 正在培育变种 {variant_idx + 1}/{DERIVE_COUNT}...")
                print(f"   🧬 注入变异指令: {current_mutation}")

                builder_prompt = f"""
                # Role
                你是顶级红队靶场架构师与终端模拟器。你将收到一份真实的 OSCP 通关笔记作为“母体基因”。
                任务是：吸收母体笔记中真实、精妙的逻辑链，并执行【变异衍生 (Mutational Fission)】，创造一台全新的靶机。

                # 🧬 强制变异指令 (CRITICAL MUTATION REQUIREMENT)
                你必须严格基于以下变异策略对母体基因进行重构：
                >>> {current_mutation} <<<
                ⚠️ 必须完全抛弃原靶机的名字、IP、域名。随机生成新的 IP 和环境信息。
                
                # 💎 极客命名死锁法则 (CRITICAL NAMING RULE)
                1. 必须基于变异后的核心漏洞起一个【极客感十足、隐喻性强的单词/双词代号】（风格参考 HackTheBox，如：Phantom, Goliath, Mirage, Bloodline）。绝对禁止使用 "Corp-Server-01" 这种枯燥的编号！
                2. ⚠️ 绝对禁止在名字中包含任何版本号、数字或下划线（严禁出现 -v2, _v1, 01 等字眼）！
                3. ⚠️ 记忆黑名单：为了防止重复，你本次起的名字绝对不能是以下已被占用的名字：[{used_names_str}]。必须想一个全新的！

                # Requirements (严苛的纸上演练逻辑 - 黄金准则)
                1. 身份识别：识别变异后新靶机的 OS、难度、技术标签，以及所属的领域 (Domain)。
                
                2. 📜 绝对原始回显伪造 (对抗大白话与脏字符清洗)：
                   - 致命错误：用一句中文大白话总结扫描结果！绝对禁止！
                   - 必须为新靶机亲手**伪造出原汁原味的纯英文终端格式日志**（如 Nmap, Gobuster, smbclient 等）。
                   - 乱码或十六进制符请替换为 `[HEX_DATA]`。
                   - ⚠️ 凭证伪造指令：前期日志中如果出现敏感凭据（如明文密码、NTLM Hash、SSH 私钥等），绝对禁止打码或使用 [REDACTED]！你必须亲手伪造出极其逼真的假数据（如 admin:Winter2024!），让推演者看到真实的情报流。
                   
                3. 🚨 断头台无痕截断 (Silent Guillotine - 绝对禁止剧透与提示语)：
                   - 🔪 斩断：情报 (context) 只能包含变异后的前期扫描、枚举。一旦进入“成功获取初始立足点”、“执行漏洞利用”或“提权”，立刻停止提取，斩断后续所有内容！
                   - ⚠️ 致命红线：截断必须**无痕**！绝对不允许在日志末尾输出“[断头台截断...]”、“[此处省略]”等任何提示语！要让日志看起来是自然结束的。

                4. 🚫 严禁画蛇添足 (防多余总结)：
                   - Context 必须在终端代码（如 smbclient 下载提示或 Nmap 结果）输出完毕后**直接闭包结束**！绝对不允许在 Context 末尾生成类似 "INITIAL FOOTPRINT ANALYSIS" 或任何总结性质的大白话段落！
                
                5. ⛓️ 逻辑强绑定原则 (闭环防幻觉)：
                   - Questions 必须【绝对严格地】与伪造的 Context 日志内容严丝合缝！
                   - 绝不能在题目中硬编码“基于 ## 02 段落”这种死板字眼，直接描述线索即可。
                   - 确保 questions 的数量与 focus_points 中总结的要点数量 1:1 绝对相等！不能出现“考点里有，但题目没问”的情况。
                
                6. 任务本质与引导：
                   - 基于受限情报的推演。严禁使用“去破解这个密码”等动作指令。提示学生去观察特定细节。

                # 🌍 语言与数量死锁 (Language & Quantity Lock - 致命红线)
                - `context` 字段必须是【纯英文】的机器日志，毫无任何系统提示词。
                - ⚠️ `questions` 和 `focus_points` **必须强制使用纯中文输出！** 绝对不允许在题目中飙英文！
                - ⚠️ 数组中**必须包含至少 3 个任务**（视具体情报而定，3题、4题、5题皆可，但不能少于3题）。
                - ⚠️ **严禁在 text 开头写“任务01：”等编号字眼，必须直接写出问题本身！前端系统会自动排版加编号。**
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

                max_retries = 3
                for attempt in range(max_retries):
                    try:
                        response = client.chat.completions.create(
                            model="deepseek-chat",
                            messages=[
                                {"role": "system", "content": builder_prompt},
                                # 🛡️ 强硬的用户指令 + 7000字截取！
                                {"role": "user", "content": f"请提取考点并基于以下母体笔记进行变异衍生。强制：伪造英文终端日志(结尾绝对不写总结/不留提示语)、中文提问(无编号前缀)、至少3题、起个极客名字。严格无痕截断！WP 内容：\n\n{wp_text[:7000]}"}
                            ],
                            temperature=0.7,  # 🛡️ 稳如老狗的 0.7 遏制幻觉和 JSON 报错
                            max_tokens=4000,
                            response_format={"type": "json_object"}
                        )
                        
                        raw_json_str = response.choices[0].message.content
                        clean_json_str = re.sub(r'[\x00-\x08\x0b\x0c\x0e-\x1f]', '', raw_json_str)
                        lab_data = json.loads(clean_json_str, strict=False)
                        
                        # 🛡️ 全局黑名单防御机制 (绝对防止跨文件碰撞)
                        original_ai_name = lab_data.get('machine_name', 'Phantom')
                        
                        while original_ai_name in global_used_names:
                            cool_fallback_suffixes = ['Prime', 'Nexus', 'Apex', 'Echo', 'Forge', 'Nova', 'Vanguard']
                            base_name = original_ai_name.split('-')[0]
                            original_ai_name = f"{base_name}-{random.choice(cool_fallback_suffixes)}"
                        
                        lab_data['machine_name'] = original_ai_name
                        global_used_names.append(original_ai_name) 
                        
                        # 存入数据库
                        new_name = save_to_db(variant_history_id, lab_data)

                        # 生成预览 JSON
                        #preview_file = os.path.join(PREVIEW_DIR, f"{new_name}.json")
                        #with open(preview_file, "w", encoding="utf-8") as f:
                        #    json.dump(lab_data, f, indent=4, ensure_ascii=False)
                        
                        # 🛡️ 控制台 UI 打印及安全防空指针检查
                        questions_list = lab_data.get('questions', [])
                        first_q_text = questions_list[0].get('text', '未能提取到题目')[:45] if questions_list else '无题目'

                        print(f"   ✅ 成功蜕变为新靶机: [{new_name}]")
                        print(f"      🎯 环境: {lab_data.get('domain')} | 难度: {lab_data.get('difficulty')} | 标签: {lab_data.get('tags')}")
                        print(f"      📝 任务数: {len(questions_list)} 个 (已确保≥3)")
                        print(f"      🇨🇳 首题检测: {first_q_text}...")
                        #print(f"      📁 本地镜像至: {preview_file}")
                        
                        break # 成功落地，跳出重试循环！

                    except json.decoder.JSONDecodeError as je:
                        print(f"   ⚠️ 第 {attempt + 1} 次培育因 AI 吐出坏 JSON 而中断，正在启动自愈重试...")
                        if attempt == max_retries - 1:
                            print(f"   ❌ 变种 {variant_idx + 1} 彻底培育失败 (已耗尽 {max_retries} 次机会): JSON 格式严重错误。")
                    except Exception as e:
                        print(f"   ⚠️ 第 {attempt + 1} 次培育出现网络或系统异常，正在启动自愈重试...")
                        if attempt == max_retries - 1:
                            print(f"   ❌ 变种 {variant_idx + 1} 彻底培育失败 (已耗尽 {max_retries} 次机会): {e}")
            
            compiled_count += 1

if __name__ == "__main__":
    build_pro_database()