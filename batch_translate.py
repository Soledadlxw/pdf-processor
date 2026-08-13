#!/usr/bin/env python3
"""Phase 2: 批量翻译英文笔记 → 中文（仅 qwen3.6，无模型切换）
同时翻译章节标题、提炼短标签、重命名文件为中文"""

import os
import sys
import re
import yaml
import time
from ollama import Client
from pathlib import Path

MODEL = "qwen3.6:35b-mlx"
# 与 configs/medical.yaml 的 child_chunk_size 保持一致
CHILD_CHUNK_SIZE = 4000

STAGE2_PROMPT = open(os.path.join(os.path.dirname(__file__),
                     "prompts/medical_stage2.txt")).read()


def translate_title(ollama: Client, chapter_title: str, book_zh: str) -> tuple[str, str]:
    """翻译章节标题 + 提炼 5-8 字短标签，返回 (title_zh, short_title)"""
    prompt = f"""Translate this medical textbook chapter title into Chinese, then extract a 5-8 character short label.

Chapter title: {chapter_title}
Book: {book_zh}

Rules:
- title_zh: accurate Chinese translation of the full title (keep key terms)
- short_title: 5-8 Chinese characters capturing the core topic, suitable for navigation links

Output format (exactly):
title_zh: <Chinese translation>
short_title: <5-8 char label>"""

    try:
        resp = ollama.chat(model=MODEL, messages=[{"role": "user", "content": prompt}],
                          options={"temperature": 0.1, "num_ctx": 2048}, keep_alive="0s")
        text = resp["message"]["content"]
        tz = re.search(r'title_zh:\s*(.+)', text)
        st = re.search(r'short_title:\s*(.+)', text)
        return (tz.group(1).strip() if tz else chapter_title,
                st.group(1).strip()[:10] if st else chapter_title[:10])
    except Exception:
        return (chapter_title, chapter_title[:10])


def slugify_cn(text: str) -> str:
    """中文友好的文件名 slug"""
    text = re.sub(r'[^\w一-鿿\s-]', '', text)
    text = re.sub(r'[-\s]+', '-', text)
    return text.strip('-')[:80]


def read_frontmatter(path: Path) -> dict:
    """读 Markdown 的 YAML frontmatter，读不出来就当空的。"""
    try:
        content = path.read_text()
        if not content.startswith("---"):
            return {}
        return yaml.safe_load(content[3:content.index("---", 3)]) or {}
    except (OSError, ValueError, yaml.YAMLError):
        return {}


def split_sections(text: str, chunk_size: int) -> list[str]:
    """按 ## 小节聚合成不超过 chunk_size 的块。

    每块都是原文的逐字切片：re.split 消耗掉的正好是一个 "\\n"，所以用 "\\n"
    拼回去就能还原原文。
    """
    sections = re.split(r"\n(?=## )", text)
    chunks: list[str] = []
    current: list[str] = []
    for sec in sections:
        if current and len("\n".join(current + [sec])) > chunk_size:
            chunks.append("\n".join(current).strip())
            current = [sec]
        else:
            current.append(sec)
    if current:
        chunks.append("\n".join(current).strip())
    return [c for c in chunks if c]


def rebuild_children(parent_text: str, parent_fm: dict, old_parent_name: str,
                     parent_path: Path,
                     chunk_size: int = CHILD_CHUNK_SIZE) -> list[Path]:
    """用翻译好的父笔记重建子文档。

    子文档必须是父笔记的逐字切片——「子文档命中、父文档供上下文」这套检索方式
    的前提就是这个。原来的做法是把 Phase 1 留下的英文子文档再各自送一次 LLM，
    于是同一份内容被翻译两遍：token 量差不多翻倍，而且独立翻出来的措辞和父笔记
    对不上，引用会漂移。
    """
    chunks_dir = parent_path.parent / "_chunks"
    if not parent_text.strip():
        return []
    chunks_dir.mkdir(exist_ok=True)

    # 先清掉这个父笔记的旧子文档（英文内容，文件名也还是旧的）
    stale = {old_parent_name, parent_path.name}
    for cf in chunks_dir.glob("*.md"):
        if read_frontmatter(cf).get("child_of") in stale:
            cf.unlink()

    written = []
    for i, body in enumerate(split_sections(parent_text, chunk_size), start=1):
        fm = dict(parent_fm)
        fm["child_of"] = parent_path.name
        fm["parent"] = parent_path.name
        fm["child_index"] = i
        path = chunks_dir / f"{parent_path.stem}-{i}.md"
        yaml_fm = yaml.dump(fm, allow_unicode=True, default_flow_style=False).strip()
        path.write_text(f"---\n{yaml_fm}\n---\n\n{body}")
        written.append(path)
    return written


def translate_note(note_path: str, ollama: Client) -> bool:
    """翻译单篇英文笔记为中文，翻译标题，重命名为中文文件名"""
    with open(note_path) as f:
        content = f.read()

    # 解析 frontmatter
    fm = {}
    body = content
    if content.startswith("---"):
        end = content.index("---", 3)
        fm = yaml.safe_load(content[3:end]) or {}
        body = content[end+3:]

    # 检查是否已翻译且有中文标题（跳过）
    if fm.get("mode") in ("full", "bilingual") and fm.get("title_zh"):
        return False
    if "TL;DR" not in body and "##" not in body:
        return False  # 空壳

    book_zh = fm.get("book_zh", fm.get("book", ""))
    book_title = fm.get("book", "")
    cite_key = fm.get("cite_key", "")
    chapter_title = fm.get("chapter_title", fm.get("title", ""))
    year = fm.get("year", "")
    edition = fm.get("edition", "")
    dsm_version = fm.get("dsm_version", "")

    # 翻译标题 + 提炼短标签
    title_zh, short_title = translate_title(ollama, chapter_title, book_zh)

    MAX_CHUNK_CHARS = 15000

    if len(body) <= MAX_CHUNK_CHARS:
        chunks = [body]
    else:
        sections = body.split("\n## ")
        chunks = []
        current = sections[0] if sections else ""
        for sec in sections[1:]:
            sec_full = "\n## " + sec
            if len(current) + len(sec_full) < MAX_CHUNK_CHARS:
                current += sec_full
            else:
                if current.strip():
                    chunks.append(current.strip())
                current = sec_full
        if current.strip():
            chunks.append(current.strip())

    all_translations = []
    for ci, chunk in enumerate(chunks):
        chunk_title = f"{chapter_title} ({ci+1}/{len(chunks)})" if len(chunks) > 1 else chapter_title
        prompt = STAGE2_PROMPT.format(
            book_zh=book_zh, book_title=book_title,
            year=year, edition=edition,
            cite_key=cite_key, chapter_title=chunk_title,
            dsm_version=dsm_version,
            english_summary=chunk,
        )
        for attempt in range(3):
            try:
                resp = ollama.chat(model=MODEL, messages=[
                    {"role": "user", "content": prompt}
                ], options={"temperature": 0.2, "num_ctx": 16384}, keep_alive="0s")
                all_translations.append(resp["message"]["content"])
                break
            except Exception as e:
                if attempt < 2:
                    time.sleep(30)
                else:
                    print(f"  ❌ chunk {ci+1} failed")
                    return False

    chinese = "\n\n".join(all_translations)

    # 更新 frontmatter + 写回
    fm["mode"] = "full"
    fm["stage2_model"] = MODEL
    fm["title_zh"] = title_zh
    fm["short_title"] = short_title

    # 用中文标题重命名文件
    old_path = Path(note_path)
    ch_num = fm.get("chapter", 0)
    new_slug = slugify_cn(title_zh)
    new_name = f"{ch_num:02d}-{new_slug}.md" if ch_num else f"{new_slug}.md"
    new_path = old_path.parent / new_name

    yaml_fm = yaml.dump(fm, allow_unicode=True, default_flow_style=False).strip()
    with open(note_path, "w") as f:
        f.write(f"---\n{yaml_fm}\n---\n\n{chinese}")

    # 重命名主文件
    final_path = old_path
    if new_path != old_path:
        try:
            old_path.rename(new_path)
            final_path = new_path
        except OSError as e:
            print(f"⚠️  重命名失败（{e}），保留 {old_path.name}", end=" ")

    # 重建 _chunks/ 子文档：直接切中文父文本，不再逐个送 LLM
    rebuild_children(chinese, fm, old_path.name, final_path)

    # 重生成面包屑导航（链接指向中文文件名）
    all_notes = sorted(old_path.parent.glob("*.md"))
    all_notes = [n for n in all_notes if not n.name.startswith("_")]
    prev_note, next_note = None, None
    for j, np_path in enumerate(all_notes):
        if np_path == final_path:
            if j > 0:
                prev_note = all_notes[j-1]
            if j < len(all_notes) - 1:
                next_note = all_notes[j+1]
            break

    nav = ""
    if prev_note:
        p_label = read_frontmatter(prev_note).get("short_title", prev_note.stem)
        nav += f"← [[{prev_note.stem}|{p_label}]]  "
    if next_note:
        n_label = read_frontmatter(next_note).get("short_title", next_note.stem)
        nav += f"|  [[{next_note.stem}|{n_label}]] →"

    # 写回文件，在 # Title 后插入导航
    if nav:
        final_content = final_path.read_text()
        # 在第一个 # 标题行后插入导航
        title_end = final_content.find("\n", final_content.find("# "))
        if title_end > 0:
            final_content = final_content[:title_end+1] + "\n" + nav + "\n" + final_content[title_end+1:]
            final_path.write_text(final_content)

    return True


def main():
    note_dir = sys.argv[1] if len(sys.argv) > 1 else None
    if not note_dir:
        print("Usage: python3 batch_translate.py <notes_directory>")
        sys.exit(1)

    notes = sorted(Path(note_dir).glob("*.md"))
    notes = [n for n in notes if not n.name.startswith("_")]

    ollama = Client(host="http://localhost:11434", timeout=300)
    print(f"Translating {len(notes)} notes with {MODEL}...")
    print(f"Source: {note_dir}")

    done = 0
    for i, np in enumerate(notes):
        name = np.name[:60]
        print(f"  [{i+1}/{len(notes)}] {name}...", end=" ", flush=True)
        t0 = time.time()
        ok = translate_note(str(np), ollama)
        dt = time.time() - t0
        if ok:
            print(f"✅ {dt:.0f}s")
            done += 1
        else:
            print(f"⏭️  ({dt:.0f}s)")

    print(f"\nDone. {done}/{len(notes)} translated.")


if __name__ == "__main__":
    main()
