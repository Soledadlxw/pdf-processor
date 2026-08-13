#!/usr/bin/env python3
"""process_epub.py — EPUB 历史书籍 → 中文结构笔记 + RAG 父子文档

用法:
    python3 process_epub.py                  # 正常处理
    python3 process_epub.py --dry-run        # 只提取文本，不调 LLM
    python3 process_epub.py --book "name"    # 只处理匹配名称的书
"""

import zipfile
import re
import os
import sys
import time
import yaml
import json as json_lib
import urllib.request
import urllib.error
from html.parser import HTMLParser
from xml.etree import ElementTree as ET
from pathlib import Path
from datetime import datetime
from collections import OrderedDict

from ollama import Client

from notes import write_children

# ===== 配置 =====
BOOK_DIR = "./books_history"
VAULT_DIR = os.path.expanduser("~/note")
NOTE_FOLDER = "Book Notes"
META_FILE = "./metadata_history.yaml"

MODEL = "qwen3.6:35b-mlx"

MAX_CHARS_PER_CHAPTER = 30000
CHILD_CHUNK_SIZE = 5000
MAX_RETRIES = 3

OLLAMA_API = "http://localhost:11434"
ollama = Client(host=OLLAMA_API, timeout=300)


class TextExtractor(HTMLParser):
    """Extract readable text from HTML, preserving structure."""
    def __init__(self):
        super().__init__()
        self.text = []
        self.skip = False
        self.in_blockquote = False

    def handle_starttag(self, tag, attrs):
        if tag in ('script', 'style', 'head', 'title', 'meta', 'link'):
            self.skip = True
        if tag == 'blockquote':
            self.in_blockquote = True
        if tag in ('br', 'li'):
            self.text.append('\n')

    def handle_endtag(self, tag):
        if tag in ('script', 'style', 'head', 'title', 'meta', 'link'):
            self.skip = False
        if tag == 'blockquote':
            self.in_blockquote = False
        if tag in ('p', 'div', 'h1', 'h2', 'h3', 'h4', 'h5', 'h6', 'br', 'li', 'blockquote', 'section'):
            self.text.append('\n')

    def handle_data(self, data):
        if not self.skip:
            t = data.strip()
            if t:
                if self.in_blockquote:
                    self.text.append(f"> {t}")
                else:
                    self.text.append(t)


def slugify(text: str) -> str:
    text = re.sub(r"[^\w\s-]", "", text)
    text = re.sub(r"[-\s]+", "-", text)
    return text.strip("-")[:80]


def load_metadata() -> dict:
    if not os.path.exists(META_FILE):
        return {}
    with open(META_FILE) as f:
        return yaml.safe_load(f) or {}


def load_prompt(path: str) -> str:
    if not os.path.exists(path):
        return ""
    with open(path) as f:
        return f.read()


def parse_epub_toc(epub_path: str) -> list[dict]:
    """Extract structured TOC from EPUB."""
    with zipfile.ZipFile(epub_path) as z:
        ncx = z.read("OPS/fb.ncx").decode("utf-8")
        root = ET.fromstring(ncx)

    ns = {"ncx": "http://www.daisy.org/z3986/2005/ncx/"}
    nav_points = []

    for np in root.findall(".//ncx:navPoint", ns):
        label = np.find("ncx:navLabel/ncx:text", ns)
        content = np.find("ncx:content", ns)
        label_text = label.text.strip() if label is not None and label.text else ""
        src = content.get("src", "") if content is not None else ""
        play_order = int(np.get("playOrder", "0"))
        nav_points.append({
            "order": play_order,
            "title": label_text,
            "src": src,
        })

    return nav_points


def extract_epub_text(epub_path: str, src: str) -> str:
    """Extract clean text from an EPUB HTML file."""
    # src format: "OPS/chapterX.html"
    path = src.split("#")[0]  # Remove fragment
    if not path.startswith("OPS/"):
        path = f"OPS/{path}"

    with zipfile.ZipFile(epub_path) as z:
        try:
            html = z.read(path).decode("utf-8")
        except KeyError:
            return ""

    extractor = TextExtractor()
    extractor.feed(html)
    text = ' '.join(extractor.text)
    # Clean whitespace
    text = re.sub(r'\n{3,}', '\n\n', text)
    text = re.sub(r' {2,}', ' ', text)
    text = re.sub(r'\n +', '\n', text)
    return text.strip()


def build_chapter_groups(toc: list[dict], epub_path: str) -> list[dict]:
    """Build chapter groups from TOC, merging tiny chapters."""
    chapters = []
    current_part = ""

    for item in toc:
        title = item["title"]
        src = item["src"]
        order = item["order"]

        # Skip cover, TOC, and structural pages
        if order in (0, 1):
            continue

        # Detect major parts (壹, 贰, etc.)
        if any(c in title for c in "壹贰叁肆伍陆柒捌玖拾") and len(title) <= 8:
            current_part = title
            continue

        text = extract_epub_text(epub_path, src)
        if not text or len(text) < 100:
            continue

        chapters.append({
            "title": title,
            "part": current_part,
            "src": src,
            "text": text,
            "char_count": len(text),
        })

    # Merge very small chapters (like section dividers) with next chapter
    merged = []
    i = 0
    while i < len(chapters):
        ch = chapters[i]
        if ch["char_count"] < 1500 and i + 1 < len(chapters):
            # Merge with next
            next_ch = chapters[i + 1]
            merged_title = f"{ch['title']} / {next_ch['title']}"
            merged_text = f"## {ch['title']}\n\n{ch['text']}\n\n## {next_ch['title']}\n\n{next_ch['text']}"
            merged.append({
                "title": merged_title,
                "part": ch["part"],
                "src": ch["src"],
                "text": merged_text,
                "char_count": len(merged_text),
            })
            i += 2
        else:
            merged.append(ch)
            i += 1

    return merged


def chunk_text_by_paragraphs(text: str, max_chars: int) -> list[str]:
    paragraphs = text.split("\n\n")
    chunks = []
    current = ""
    for p in paragraphs:
        if len(current) + len(p) < max_chars:
            current += p + "\n\n"
        else:
            if current.strip():
                chunks.append(current.strip())
            current = p + "\n\n"
    if current.strip():
        chunks.append(current.strip())
    return chunks


def call_ollama(prompt: str, model: str = MODEL) -> str:
    """Call Ollama with retries."""
    for attempt in range(MAX_RETRIES):
        try:
            response = ollama.chat(
                model=model,
                messages=[{"role": "user", "content": prompt}],
                options={"temperature": 0.3, "num_ctx": 8192}
            )
            return response["message"]["content"]
        except Exception as e:
            if attempt < MAX_RETRIES - 1:
                print(f"    retry {attempt+1}/{MAX_RETRIES}: {e}", end=" ", flush=True)
                time.sleep(5)
            else:
                raise

    return ""


def process_epub_book(epub_path: str, book_meta: dict, dry_run: bool = False):
    """Process one EPUB book."""
    book_title = book_meta.get("title", Path(epub_path).stem)
    book_zh = book_meta.get("title_zh", book_title)
    cite_key = book_meta.get("cite_key", "??")
    authors = ", ".join(book_meta.get("authors", ["Unknown"])) if book_meta.get("authors") else "Unknown"
    year = book_meta.get("year", "")
    dynasty = book_meta.get("dynasty", "")

    prompt_template = load_prompt("./prompts/history_stage1.txt")

    print(f"\n{'='*60}")
    print(f"📖 {book_zh}  [{cite_key}]")

    # Parse EPUB
    print("  Parsing EPUB structure...")
    toc = parse_epub_toc(epub_path)
    chapters = build_chapter_groups(toc, epub_path)
    print(f"  {len(chapters)} chapters (after merging)")

    # Output dir
    safe_name = book_zh
    output_dir = os.path.join(VAULT_DIR, NOTE_FOLDER, safe_name)
    os.makedirs(output_dir, exist_ok=True)

    total = len(chapters)
    for i, ch in enumerate(chapters):
        ch_title = ch["title"]
        ch_text = ch["text"]
        char_count = ch["char_count"]
        part = ch["part"]

        print(f"\n  [{i+1}/{total}] {ch_title}")
        print(f"    {char_count:,} chars", end="", flush=True)

        if part:
            print(f"  [{part}]", end="", flush=True)

        if dry_run:
            print("  (dry-run, skip LLM)")
            continue

        # Frontmatter
        fm = {
            "book": book_title,
            "book_zh": book_zh,
            "cite_key": cite_key,
            "authors": book_meta.get("authors", []),
            "year": year,
            "dynasty": dynasty,
            "book_type": "history",
            "chapter": i + 1,
            "chapter_title": ch_title,
            "part": part,
            "char_count": char_count,
            "processed": datetime.now().strftime("%Y-%m-%d"),
            "model": MODEL,
            "child_of": None,
            "prerequisites": [],
            "tags": book_meta.get("tags", []),
        }

        # Check if too large, need sub-chunks
        if char_count > MAX_CHARS_PER_CHAPTER:
            sub_chunks = chunk_text_by_paragraphs(ch_text, MAX_CHARS_PER_CHAPTER)
            n = len(sub_chunks)
            print(f"\n    Large chapter → {n} sub-chunks", flush=True)

            all_summaries = []
            for j, sub_text in enumerate(sub_chunks):
                print(f"    Stage1 sub-chunk {j+1}/{n}...", end=" ", flush=True)
                start = time.time()
                try:
                    prompt = prompt_template.format(
                        book_zh=book_zh,
                        book_title=book_title,
                        year=year,
                        cite_key=cite_key,
                        authors=authors,
                        chapter_title=f"{ch_title} ({j+1}/{n})",
                        dynasty=dynasty,
                        chapter_text=sub_text[:15000],
                    )
                    result = call_ollama(prompt)
                    all_summaries.append(result)
                    elapsed = time.time() - start
                    print(f"{elapsed:.0f}s", flush=True)
                except Exception as e:
                    print(f"\n    ⚠️  sub-chunk {j+1} failed: {e}")
                    all_summaries.append(f"[处理失败: {e}]")

            note_body = "\n\n---\n\n".join(all_summaries)
        else:
            print("", flush=True)
            print(f"    Stage1...", end=" ", flush=True)
            start = time.time()
            try:
                prompt = prompt_template.format(
                    book_zh=book_zh,
                    book_title=book_title,
                    year=year,
                    cite_key=cite_key,
                    authors=authors,
                    chapter_title=ch_title,
                    dynasty=dynasty,
                    chapter_text=ch_text[:15000],
                )
                note_body = call_ollama(prompt)
                elapsed = time.time() - start
                print(f"{elapsed:.0f}s", flush=True)
            except Exception as e:
                print(f"\n    ⚠️  failed: {e}")
                note_body = f"[处理失败: {e}]\n\n原始文本:\n\n{ch_text[:2000]}"

        # Write note
        safe_title = slugify(ch_title)
        filename = f"{i+1:02d}-{safe_title}.md"
        filepath = os.path.join(output_dir, filename)

        with open(filepath, "w") as f:
            f.write("---\n")
            yaml.dump(fm, f, allow_unicode=True, default_flow_style=False)
            f.write("---\n\n")
            f.write(f"# {ch_title}\n\n")
            if part:
                f.write(f"> 所属篇目：{part}\n\n")
            f.write(note_body)

        # Split into child chunks for RAG
        child_paths = write_children(note_body, fm, Path(filepath), CHILD_CHUNK_SIZE)

        print(f"    ✅ → {filename}")
        if child_paths:
            print(f"    📦 {len(child_paths)} child chunks")

    # Generate index
    generate_index(output_dir, chapters, book_meta)
    print(f"\n  📂 {output_dir}")


def generate_index(output_dir: str, chapters: list[dict], book_meta: dict):
    """Generate _{书名}.md for the book."""
    book_zh = book_meta.get('title_zh', book_meta.get('title', 'index'))
    lines = [
        "---",
        f"title: {book_meta.get('title_zh', book_meta.get('title', ''))}",
        "type: book-index",
        f"cite_key: {book_meta.get('cite_key', '')}",
        f"processed: {datetime.now().strftime('%Y-%m-%d')}",
        "---",
        "",
        f"# {book_meta.get('title_zh', '')}",
        "",
        f"**作者**: {', '.join(book_meta.get('authors', []))}",
        f"**年代**: {book_meta.get('year', '')}",
        f"**朝代**: {book_meta.get('dynasty', '')}",
        "",
        "## 目录",
        "",
    ]

    current_part = ""
    for i, ch in enumerate(chapters):
        part = ch.get("part", "")
        if part and part != current_part:
            current_part = part
            lines.append(f"\n### {part}\n")
        safe_title = slugify(ch["title"])
        filename = f"{i+1:02d}-{safe_title}"
        lines.append(f"- [{ch['title']}]({filename}.md)")

    lines.extend([
        "",
        "---",
        "",
        "## 使用说明",
        "",
        "1. 笔记由 AI 自动生成，建议对照原文阅读",
        "2. 原文引用以 `> ` 标记，标注 [原文]",
        "3. 时间线表格可用于快速定位历史事件",
    ])

    safe_zh = re.sub(r'[\\/:*?"<>|]', '', book_zh)
    with open(os.path.join(output_dir, f"_{safe_zh}.md"), "w") as f:
        f.write("\n".join(lines))


def main():
    os.makedirs(BOOK_DIR, exist_ok=True)
    books = sorted(Path(BOOK_DIR).glob("*.epub"))

    if not books:
        print(f"❌ No EPUB files found in {BOOK_DIR}/")
        sys.exit(1)

    # Filter by --book argument
    dry_run = "--dry-run" in sys.argv
    book_filter = None
    for i, arg in enumerate(sys.argv):
        if arg == "--book" and i + 1 < len(sys.argv):
            book_filter = sys.argv[i + 1]
            break

    if book_filter:
        books = [b for b in books if book_filter.lower() in b.name.lower()]
        if not books:
            print(f"❌ No EPUB matching '{book_filter}' in {BOOK_DIR}/")
            sys.exit(1)

    meta = load_metadata()

    for epub_path in books:
        book_meta = meta.get(epub_path.name, {})
        if not book_meta:
            print(f"⚠️  No metadata for {epub_path.name}, using defaults")
            book_meta = {
                "title": epub_path.stem,
                "title_zh": epub_path.stem,
                "authors": ["Unknown"],
                "year": "",
                "book_type": "history",
            }

        process_epub_book(str(epub_path), book_meta, dry_run=dry_run)

    print(f"\n{'='*60}")
    print("Done.")
    print(f"Notes:   {VAULT_DIR}/{NOTE_FOLDER}/")


if __name__ == "__main__":
    main()
