#!/usr/bin/env python3
"""医学术语词典后处理 — 与 term_dict.py 等价，引擎统一调用"""

import os
import sys
import yaml
from ollama import Client
from pathlib import Path
from datetime import datetime
from collections import defaultdict

VAULT_DIR = os.path.expanduser("~/note")
NOTE_FOLDER = "Book Notes"
MODEL = "qwen3.6:35b-mlx"


def collect_notes():
    notes_dir = os.path.join(VAULT_DIR, NOTE_FOLDER)
    all_notes = []
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
            if content.startswith("---"):
                end = content.index("---", 3)
                fm = yaml.safe_load(content[3:end]) or {}
                body = content[end+3:]
            else:
                body = content
            book = Path(root).name
            all_notes.append({"file": f, "book": book, "frontmatter": fm, "body": body})
    return all_notes


def extract_terms(note, ollama):
    sections = note["body"].split("\n## ")
    samples = []
    for sec in sections[:6]:
        lines = sec.strip().split("\n")
        samples.append("\n".join(lines[:5]))
    snippet = "\n\n---\n\n".join(samples)[:3000]

    prompt = f"""Extract medical/clinical terminology pairs from this Chinese academic note.
Book: {note['book']}
For each term: English term | 推荐中译 | 其他可能译名
Focus on: diagnostic criteria, assessment tools, intervention methods, developmental concepts, pharmacology.
Output one term per line. Skip terms without clear English origin.

Text:
{snippet}

Terminology pairs:"""

    try:
        resp = ollama.chat(model=MODEL, messages=[{"role": "user", "content": prompt}],
                           options={"temperature": 0, "num_ctx": 4096})
        return resp["message"]["content"]
    except Exception as e:
        print(f"  ⚠️  {note['file']}: {e}")
        return ""


def categorize_term(english):
    el = english.lower()
    cats = {
        "核心概念": ["attention", "imitation", "play", "communication", "social", "engagement"],
        "诊断与筛查": ["diagnos", "screen", "assessment", "scale", "dsm", "ados", "m-chat"],
        "干预方法": ["intervention", "treatment", "therapy", "behavioral", "aba", "esdm", "prt"],
        "发育里程碑": ["milestone", "motor", "language", "cognitive", "adaptive"],
        "药理学": ["medication", "drug", "dose", "risperidone", "aripiprazole", "ssri"],
        "共病": ["comorbid", "adhd", "anxiety", "epilepsy", "sleep", "feeding"],
    }
    for cat, kws in cats.items():
        if any(k in el for k in kws):
            return cat
    return "其他"


def generate_doc(term_map):
    lines = [
        "---",
        f"title: 术语对照表",
        "type: terminology",
        f"generated: {datetime.now().strftime('%Y-%m-%d')}",
        "---",
        "", "# 发育行为儿科 — 术语对照表",
        "", "> 自动生成，建议人工复核。",
    ]
    categories = ["核心概念", "诊断与筛查", "干预方法", "发育里程碑", "药理学", "共病", "其他"]
    for cat in categories:
        if cat not in term_map:
            continue
        terms = term_map[cat]
        lines.append(f"\n## {cat}\n")
        lines.append("| 英文 | 推荐中译 | 其他译名 | 出现书籍 |")
        lines.append("|------|------|------|------|")
        for eng, info in sorted(terms.items(), key=lambda x: x[0].lower()):
            zh = info["main"]
            others = ", ".join(info["variants"]) if info["variants"] else "—"
            books = ", ".join(sorted(info["books"]))
            lines.append(f"| {eng} | {zh} | {others} | {books} |")
    return "\n".join(lines)


def main():
    notes_dir = os.path.join(VAULT_DIR, NOTE_FOLDER)
    if not os.path.exists(notes_dir):
        print(f"❌ {notes_dir} not found.")
        sys.exit(1)

    ollama = Client(host="http://localhost:11434", timeout=120)
    notes = collect_notes()
    if not notes:
        print("❌ No notes found.")
        sys.exit(1)
    print(f"Collecting terms from {len(notes)} notes...")

    term_map = defaultdict(lambda: {"main": "", "variants": set(), "books": set()})
    for i, note in enumerate(notes):
        print(f"  [{i+1}/{len(notes)}] {note['file'][:50]}...", end=" ", flush=True)
        raw = extract_terms(note, ollama)
        for line in raw.strip().split("\n"):
            parts = line.strip().split("|")
            if len(parts) < 2:
                continue
            eng = parts[0].strip()
            zh = parts[1].strip()
            vars_ = [v.strip() for v in parts[2].split(",") if v.strip()] if len(parts) > 2 else []
            if not eng or not zh or len(eng) < 3:
                continue
            if not term_map[eng]["main"]:
                term_map[eng]["main"] = zh
            term_map[eng]["variants"].update(vars_)
            term_map[eng]["books"].add(note["book"])
        print(f"{len(term_map)} terms")

    categorized = defaultdict(dict)
    for eng, info in term_map.items():
        cat = categorize_term(eng)
        categorized[cat][eng] = info

    output_dir = os.path.join(VAULT_DIR, NOTE_FOLDER, "_meta")
    os.makedirs(output_dir, exist_ok=True)
    doc = generate_doc(categorized)
    output_path = os.path.join(output_dir, "terms.md")
    with open(output_path, "w") as f:
        f.write(doc)
    total = sum(len(v) for v in categorized.values())
    print(f"\n✅ Terms: {output_path} ({total} terms, {len(categorized)} categories)")


if __name__ == "__main__":
    main()
