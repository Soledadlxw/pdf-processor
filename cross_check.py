#!/usr/bin/env python3
"""跨书矛盾检测 — 扫描全部笔记，检测同一主题下不同来源的结论差异"""

import os
import sys
import yaml
import ollama
from pathlib import Path
from datetime import datetime
from collections import defaultdict

VAULT_DIR = os.path.expanduser("~/note")
NOTE_FOLDER = "Book Notes"
OUTPUT_DIR = os.path.join(VAULT_DIR, NOTE_FOLDER, "_meta")
MODEL = "qwen3.6:35b-mlx"


def collect_topics() -> dict[str, list[dict]]:
    """收集所有笔记，按 ## 标题聚类主题"""
    notes_dir = os.path.join(VAULT_DIR, NOTE_FOLDER)
    topics = defaultdict(list)

    for root, dirs, files in os.walk(notes_dir):
        if "_chunks" in root or "_meta" in root:
            continue
        for f in files:
            if not f.endswith(".md") or f == "_index.md":
                continue
            path = os.path.join(root, f)
            with open(path) as fh:
                content = fh.read()

            fm = {}
            body = content
            if content.startswith("---"):
                end = content.index("---", 3)
                fm = yaml.safe_load(content[3:end]) or {}
                body = content[end + 3:]

            book = Path(root).name
            cite_key = fm.get("cite_key", "")
            year = fm.get("year", "")

            # 按 ## 切小节，每节作为一个 topic 候选项
            sections = body.split("\n## ")
            for sec in sections:
                sec = sec.strip()
                if not sec:
                    continue
                title_end = sec.find("\n")
                title = sec[:title_end].strip() if title_end > 0 else sec[:60]
                body_text = sec[title_end:].strip()[:2000] if title_end > 0 else sec

                # 跳过案例和表格章节（单独处理）
                if title.lower().startswith("案例") or title.lower().startswith("case"):
                    continue

                topics[title].append({
                    "book": book,
                    "cite_key": cite_key,
                    "year": year,
                    "file": f,
                    "body": body_text,
                })

    # 只保留在 2+ 本书中出现的主题
    return {k: v for k, v in topics.items() if len(v) >= 2}


def compare_topic(title: str, sources: list[dict]) -> str | None:
    """用 LLM 比对同一主题的不同来源"""
    sources_text = "\n\n---\n\n".join(
        f"来源 {i+1}: {s['cite_key']} ({s['year']})\n{s['body'][:1500]}"
        for i, s in enumerate(sources)
    )

    prompt = f"""You are a clinical editor checking for contradictions between medical textbook excerpts.

Topic: "{title}"

Compare the following sources and identify:

1. CONSISTENT — where all sources agree
2. DIFFERENT — where sources give different numbers/claims (note the difference)
3. OUTDATED — where an older source's claim has been superseded

Format your response as:

## {title}

### 一致的结论
- ...

### 差异
| {sources[0]['cite_key']} ({sources[0]['year']}) | {sources[1]['cite_key']} ({sources[1]['year']}) | 说明 |
|------|------|------|
| ... | ... | ... |

### 可能过时
- ...

If all sources agree, say "✅ 无差异" and skip the table.
If sources disagree, be specific about what differs.

Sources:
{sources_text}"""

    try:
        response = ollama.chat(
            model=MODEL,
            messages=[{"role": "user", "content": prompt}],
            options={"temperature": 0, "num_ctx": 8192}
        )
        content = response["message"]["content"].strip()
        # 对几个常见的过度自信模式做后过滤
        if "✅ 无差异" in content or "all sources agree" in content.lower():
            # 但如果有数字差异，仍然输出（后续改 prompt 很难完美覆盖，先保留）
            return content if len(content) > 50 else None
        return content
    except Exception as e:
        print(f"  ⚠️  {title[:40]}: {e}")
        return None


def generate_conflicts_doc(results: list[str]) -> str:
    """生成矛盾报告"""
    lines = [
        "---",
        "title: 跨书差异报告",
        "type: cross-book-conflicts",
        f"generated: {datetime.now().strftime('%Y-%m-%d')}",
        "---",
        "",
        "# 跨书差异与一致性报告",
        "",
        "> 自动生成，建议人工逐条核实。",
        "> 差异不等于错误——有时是新证据更新了旧结论。",
        "",
    ]

    conflicts = [r for r in results if r and "✅ 无差异" not in r]
    consistent = [r for r in results if r and "✅ 无差异" in r]

    if conflicts:
        lines.append(f"## ⚠️ 发现 {len(conflicts)} 处差异\n")
        for r in conflicts:
            lines.append(r)
            lines.append("\n---\n")

    if consistent:
        lines.append(f"## ✅ 一致的结论（{len(consistent)} 项）\n")
        lines.append("以下主题在所有来源中结论一致：\n")
        for r in consistent:
            title = r.split("\n")[0].replace("## ", "").strip()
            lines.append(f"- {title}")

    lines.extend([
        "",
        "---",
        "",
        "## 使用建议",
        "",
        "1. 差异处以**最新年份**的来源为准（优先 2020 年后出版的资料）",
        "2. 干预方法的差异可能反映证据积累——查阅近 3 年的 meta 分析做最终判断",
        "3. 诊断标准的差异通常由 DSM 版本更新驱动——以 DSM-5-TR (2022) 为准",
        "",
    ])

    return "\n".join(lines)


def main():
    notes_dir = os.path.join(VAULT_DIR, NOTE_FOLDER)
    if not os.path.exists(notes_dir):
        print(f"❌ {notes_dir} not found.")
        sys.exit(1)

    print("Collecting topics...")
    topics = collect_topics()
    print(f"  {len(topics)} topics appear in 2+ books")

    if not topics:
        print("❌ Need at least 2 books processed to detect conflicts.")
        sys.exit(1)

    results = []
    # 只分析前 50 个主题（控制 LLM 调用量）
    topics_to_analyze = list(topics.items())[:50]

    total = len(topics_to_analyze)
    for i, (title, sources) in enumerate(topics_to_analyze):
        book_list = ", ".join(f"{s['cite_key']}({s['year']})" for s in sources)
        print(f"  [{i+1}/{total}] {title[:50]}... [{book_list}]", end=" ", flush=True)
        result = compare_topic(title, sources)
        if result:
            results.append(result)
            has_diff = "✅ 无差异" not in result
            print("⚠️" if has_diff else "✅")
        else:
            print("skipped")

    os.makedirs(OUTPUT_DIR, exist_ok=True)
    doc = generate_conflicts_doc(results)
    output_path = os.path.join(OUTPUT_DIR, "conflicts.md")

    with open(output_path, "w") as f:
        f.write(doc)

    conflicts_count = sum(1 for r in results if "✅ 无差异" not in r)
    print(f"\n✅ Conflicts report: {output_path}")
    print(f"   {conflicts_count} conflicts, {len(results) - conflicts_count} consistent")


if __name__ == "__main__":
    main()
