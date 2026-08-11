#!/usr/bin/env python3
"""历史类后处理 — 生成人物关系图、事件脉络图、帝系传承图"""

import os, sys, yaml, json, re, time
from pathlib import Path
from datetime import datetime
from collections import defaultdict
from ollama import Client

VAULT_DIR = os.path.expanduser("~/note")
NOTE_FOLDER = "Book Notes"
MODEL = "qwen3.6:35b-mlx"
ollama = Client(host="http://localhost:11434", timeout=300)


def collect_notes(book_dir: str) -> list[dict]:
    """收集一本书的所有笔记"""
    notes = []
    for f in sorted(Path(book_dir).glob("*.md")):
        if f.name == "_index.md":
            continue
        content = f.read_text()
        fm = {}
        body = content
        if content.startswith("---"):
            end = content.index("---", 3)
            fm = yaml.safe_load(content[3:end]) or {}
            body = content[end+3:]
        notes.append({"file": f.name, "frontmatter": fm, "body": body})
    return notes


def query_llm(prompt: str) -> str:
    """调用 LLM，带重试"""
    for attempt in range(3):
        try:
            resp = ollama.chat(model=MODEL, messages=[{"role":"user","content":prompt}],
                              options={"temperature": 0.2, "num_ctx": 8192})
            return resp["message"]["content"]
        except Exception as e:
            if attempt < 2:
                time.sleep(5)
            else:
                raise
    return ""


def generate_character_graph(notes: list[dict], book_name: str) -> str:
    """生成人物关系 Mermaid 图 — 扫描全书所有人物（包括事件中提及的）"""
    all_characters = {}  # {name: {part, chapters, summary, from_event}}

    for note in notes:
        body = note["body"]
        fm = note.get("frontmatter", {})
        part = fm.get("part", "")
        ch_title = fm.get("chapter_title", "")

        # 来源1: ## 重要人物 段落
        m = re.search(r'## 重要人物\n(.*?)(?=\n## |\Z)', body, re.DOTALL)
        if m:
            char_blocks = re.split(r'\n(?=### )', m.group(1))
            for block in char_blocks:
                name_m = re.match(r'###\s*\[人物\]\s*(.+)', block)
                if not name_m:
                    continue
                name = name_m.group(1).strip()
                if len(name) > 20:
                    continue
                if name not in all_characters:
                    all_characters[name] = {"part": part, "chapters": [], "summary": "", "from_event": False}
                all_characters[name]["chapters"].append(ch_title)
                if not all_characters[name]["summary"]:
                    all_characters[name]["summary"] = block[:300].replace('\n', ' ')

        # 来源2: ## 关键事件 段落 — 提取事件中涉及的人物名
        m2 = re.search(r'## 关键事件\n(.*?)(?=\n## |\Z)', body, re.DOTALL)
        if m2:
            event_text = m2.group(1)
            # 从事件中提取人物标签：**人物**：... 或 - **人物**：...
            for line in event_text.split('\n'):
                people_m = re.search(r'\*\*人物[：:]\*\*\s*(.+)', line)
                if people_m:
                    names_str = people_m.group(1)
                    # 分割人名（逗号、顿号、空格分隔）
                    for n in re.split(r'[，,、\s]+', names_str):
                        n = n.strip()
                        if n and len(n) < 20 and n not in ('—', '等', '无'):
                            if n not in all_characters:
                                all_characters[n] = {"part": part, "chapters": [], "summary": "", "from_event": True}
                            all_characters[n]["chapters"].append(ch_title)

    # 区分来源
    from_char_section = {k: v for k, v in all_characters.items() if not v["from_event"]}
    from_event_only = {k: v for k, v in all_characters.items() if v["from_event"] and len(v["chapters"]) >= 2}

    print(f"    Characters from 重要人物 sections: {len(from_char_section)}")
    print(f"    Characters from 关键事件 sections (2+ appearances): {len(from_event_only)}")
    print(f"    Total unique: {len(all_characters)}")

    if len(all_characters) < 10:
        return ""

    # 按篇目分组
    char_by_part = defaultdict(list)
    for name, info in all_characters.items():
        char_by_part[info["part"]].append(name)

    char_list_text_parts = []
    for part, names in char_by_part.items():
        char_list_text_parts.append(f"【{part}】{', '.join(names)}")
    char_list_text = "\n".join(char_list_text_parts)

    # 详细信息：优先有结构化摘要的人物
    detail_texts = []
    for name, info in from_char_section.items():
        detail_texts.append(f"{name}[{info['part']}]: {info['summary']}")
    # 补充事件中出现的人物
    for name, info in from_event_only.items():
        if name not in from_char_section:
            detail_texts.append(f"{name}[{info['part']}]: 出现于 {', '.join(info['chapters'][:3])}")

    detail_text = "\n\n".join(detail_texts[:100])

    prompt = f"""你是一名历史数据整理专家。请为《{book_name}》生成**全书完整人物关系图**。

⚠️ 核心要求：以**明朝16位皇帝为骨架**，全部皇帝必须出现。然后围绕每个皇帝，添加书中出现的所有重要臣子、对手、亲属。

明朝16帝：朱元璋(洪武) → 朱允炆(建文) → 朱棣(永乐) → 朱高炽(洪熙) → 朱瞻基(宣德) → 朱祁镇(正统/天顺) → 朱祁钰(景泰) → 朱见深(成化) → 朱祐樘(弘治) → 朱厚照(正德) → 朱厚熜(嘉靖) → 朱载坖(隆庆) → 朱翊钧(万历) → 朱常洛(泰昌) → 朱由校(天启) → 朱由检(崇祯)

以下是书中出现的**全部人物清单**（按篇目分组），请确保重要人物都不遗漏：

{char_list_text}

以下是部分人物的详细关系信息：

{detail_text[:12000]}

要求：
1. 16位皇帝全部出现，用继承关系串联：朱元璋→朱允炆→朱棣→...→朱由检
2. 围绕每个皇帝添加其重要臣子、对手、亲属（从上面的人物清单中选）
3. 按时期分 4-5 个 subgraph，不要使用 %% 注释
4. 关系线标注：君臣、父子、敌对、盟友、师徒、继承、兄弟、夫妻 等
5. 图中人物尽量多，能把清单里的重要人物都放进去
6. 只输出 Mermaid 代码块和简要说明

输出格式：
```mermaid
graph TD
    subgraph "时期名"
        朱元璋 -->|继承| 朱允炆
        朱元璋 -->|君臣| 徐达
        ...
    end
```"""

    result = query_llm(prompt)
    return result


def generate_event_timeline(notes: list[dict], book_name: str) -> str:
    """生成事件脉络 Mermaid 图 — 从全书均匀采样"""
    event_snippets = []
    for note in notes:
        body = note["body"]
        fm = note.get("frontmatter", {})
        part = fm.get("part", "")
        ch_title = fm.get("chapter_title", "")
        # 收集关键事件 + 本章概要
        m = re.search(r'## 关键事件\n(.*?)(?=\n## |\Z)', body, re.DOTALL)
        if m:
            event_snippets.append(f"[{part}] {ch_title}\n{m.group(1)[:1200]}")
        else:
            m2 = re.search(r'## 本章概要\n(.*?)(?=\n## |\Z)', body, re.DOTALL)
            if m2:
                event_snippets.append(f"[{part}] {ch_title}\n{m2.group(1)[:800]}")

    total = len(event_snippets)
    if total < 5:
        return ""

    # 均匀采样：从全书开头、中间、结尾各取 1/3
    third = max(total // 3, 1)
    sampled = []
    # 开头1/3
    sampled.extend(event_snippets[:third])
    # 中间1/3
    sampled.extend(event_snippets[third:2*third])
    # 结尾1/3
    sampled.extend(event_snippets[2*third:])

    # 去重后合并，控制总长度
    seen = set()
    unique = []
    for s in sampled:
        key = s[:60]
        if key not in seen:
            seen.add(key)
            unique.append(s)

    combined = "\n\n---\n\n".join(unique)
    # 尽量多送内容给 LLM
    text_to_send = combined[:15000]

    prompt = f"""你是一名历史数据整理专家。根据以下关于《{book_name}》的笔记，生成**全书完整历史事件脉络图**。

要求：
1. 必须覆盖全书整个时间跨度，从元末(1328)到明末(1644)，不要只写前期
2. 按时间顺序分 5-6 个时期组织
3. 用 Mermaid graph LR 语法（不要用 timeline）
4. 每个节点用 [年份: 事件简述] 格式，年代必须明确
5. 至少 35 个关键事件节点，均匀分布在整个明朝三百年
6. 节点之间用 --> 连接表示时间顺序
7. 每个 subgraph 代表一个时期，每行 chain 同期的 4-5 个事件
8. 不要使用 %% 注释
9. 只输出 Mermaid 代码块和简要说明

笔记内容（已均匀采样自全书开头/中间/结尾）：
{text_to_send}

输出格式：
```mermaid
graph LR
    subgraph "时期名(年份范围)"
        A[年份: 事件] --> B[年份: 事件] --> C[年份: 事件]
    end
    subgraph "时期名(年份范围)"
        D[年份: 事件] --> E[年份: 事件] --> F[年份: 事件]
    end
    ...
```"""

    result = query_llm(prompt)
    return result


def generate_emperor_line(notes: list[dict], book_name: str) -> str:
    """生成帝系传承图"""
    # 从 frontmatter 和 body 中收集皇帝信息
    emperor_text = []
    for note in notes:
        body = note["body"]
        fm = note.get("frontmatter", {})
        chapter_title = fm.get("chapter_title", "")
        part = fm.get("part", "")

        # 收集包含"皇帝"或具体帝王名的段落
        if any(kw in body for kw in ["皇帝", "太祖", "成祖", "仁宗", "宣宗", "英宗",
                                        "代宗", "宪宗", "孝宗", "武宗", "世宗", "穆宗",
                                        "神宗", "光宗", "熹宗", "思宗", "洪武", "永乐",
                                        "洪熙", "宣德", "正统", "景泰", "天顺", "成化",
                                        "弘治", "正德", "嘉靖", "隆庆", "万历", "泰昌",
                                        "天启", "崇祯", "建文", "朱元璋", "朱棣"]):
            emperor_text.append(f"[{part}] {chapter_title}\n{body[:800]}")

    combined = "\n\n".join(emperor_text[:30])

    if len(combined) < 300:
        return ""

    prompt = f"""你是一名历史数据整理专家。根据《{book_name}》的笔记，生成明朝帝系传承图。

要求：
1. 列出明朝16位皇帝（含追尊）
2. 用 Mermaid graph TD 语法
3. 标注：庙号、姓名、年号、在位时间
4. 在 Mermaid 代码块第一行加上 `%%{{init: {{"theme": "base", "themeVariables": {{"fontSize": "16px"}}}}}}%%` 来放大字体
5. 只输出 Mermaid 代码块和简要说明

笔记内容：
{combined[:6000]}

输出格式（节点内只用中文姓名，不要加 br 和特殊符号）：
```mermaid
graph TD
    朱元璋[明太祖 朱元璋 洪武1368-1398] --> 朱允炆[建文帝 朱允炆 建文1398-1402]
    ...
```"""

    result = query_llm(prompt)
    return result


def generate_summary_doc(notes: list[dict], book_name: str, all_charts: dict) -> str:
    """生成汇总文档"""
    lines = [
        "---",
        f"title: {book_name} — 关系图谱",
        "type: history-graphs",
        f"generated: {datetime.now().strftime('%Y-%m-%d')}",
        "---",
        "",
        f"# {book_name} — 全书关系图谱",
        "",
        "> 自动生成，建议对照原文使用。",
        "",
    ]

    sections = [
        ("人物关系图", "character"),
        ("帝系传承图", "emperor"),
        ("事件脉络图", "event"),
    ]

    for title, key in sections:
        content = all_charts.get(key, "")
        if not content:
            continue
        lines.append(f"## {title}")
        lines.append("")
        # Clean up Mermaid blocks for Obsidian compatibility
        if "```mermaid" in content:
            # Remove init directives
            content = re.sub(r'%%\{[Ii]nit:.*?\}%%\s*', '', content)
            # Remove %% comment lines
            content = re.sub(r'\n\s*%% .*', '', content)
            # Quote unquoted subgraph labels
            content = re.sub(
                r'subgraph (["\']?)([^"\'\n]+)\1',
                lambda m: f'subgraph "{m.group(2)}"',
                content
            )
            # Remove <br/> and <br> (breaks Obsidian Mermaid)
            content = re.sub(r'<br\s*/?>', ' ', content)
            # Remove | inside node labels (reliable Mermaid compatibility)
            content = re.sub(r'\[([^\]]*)\|([^\]]*)\]', r'[\1 \2]', content)
            lines.append(content.strip())
        else:
            lines.append(content.strip())
        lines.append("")
        lines.append("---")
        lines.append("")

    lines.extend([
        "",
        "## 使用说明",
        "",
        "1. Mermaid 图表可在 Obsidian 中直接渲染（需开启 Mermaid 支持）",
        "2. 也可复制到 https://mermaid.live 查看",
        "3. 人物关系以书中视角为准，主要反映作者当年明月的叙述逻辑",
    ])

    return "\n".join(lines)


def main():
    # 找到历史类书籍目录
    notes_base = os.path.join(VAULT_DIR, NOTE_FOLDER)
    history_dirs = []

    for d in sorted(Path(notes_base).iterdir()):
        if not d.is_dir() or d.name.startswith("_") or d.name.startswith("."):
            continue
        # 检测是否为历史类：看 _index.md 中的 book_type
        index_file = d / "_index.md"
        if index_file.exists():
            content = index_file.read_text()
            if "history" in content.lower() or "明朝" in content:
                history_dirs.append(d)

    if not history_dirs:
        print("No history book directories found.")
        sys.exit(1)

    for book_dir in history_dirs:
        book_name = book_dir.name
        print(f"\n{'='*60}")
        print(f"📊 {book_name} — 生成关系图谱")

        notes = collect_notes(str(book_dir))
        print(f"  {len(notes)} notes loaded")

        charts = {}

        # 1. 帝系传承（最重要的基础图）
        print("  Generating emperor lineage...", end=" ", flush=True)
        try:
            charts["emperor"] = generate_emperor_line(notes, book_name)
            print("✅")
        except Exception as e:
            print(f"❌ {e}")

        # 2. 人物关系
        print("  Generating character graph...", end=" ", flush=True)
        try:
            charts["character"] = generate_character_graph(notes, book_name)
            print("✅")
        except Exception as e:
            print(f"❌ {e}")

        # 3. 事件脉络
        print("  Generating event timeline...", end=" ", flush=True)
        try:
            charts["event"] = generate_event_timeline(notes, book_name)
            print("✅")
        except Exception as e:
            print(f"❌ {e}")

        # 生成汇总文档
        doc = generate_summary_doc(notes, book_name, charts)
        output_path = book_dir / "_graphs.md"
        with open(output_path, "w") as f:
            f.write(doc)

        count = sum(1 for v in charts.values() if v)
        print(f"  ✅ _graphs.md ({count}/3 graphs generated)")


if __name__ == "__main__":
    main()
