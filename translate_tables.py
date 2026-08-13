#!/usr/bin/env python3
"""translate_tables.py — 纯表格书籍逐页翻译（仅 qwen，不调 gemma4）

用法: python3 translate_tables.py <pdf_path> <output_dir>
"""

import fitz
import sys
import os
import re
import time
from pathlib import Path
from datetime import datetime
from ollama import Client

MODEL = "qwen3.6:35b-mlx"
NUM_CTX = 16384

# 一批送进模型的字符上限。不能按固定页数分批再截断——密排的常模表一页就有
# 五六千字符，10 页合起来能到 5 万字，截断之后后面几页连页标记都进不了 prompt，
# 于是既不翻译也不落文件，而且不报错。
MAX_BATCH_CHARS = 12000
MAX_BATCH_PAGES = 10
# 单页超过这个长度时，即使自己单独成批也可能顶满上下文窗口
SOLO_PAGE_WARN_CHARS = 30000


def build_batches(pages: list[tuple[int, str]],
                  max_chars: int = MAX_BATCH_CHARS,
                  max_pages: int = MAX_BATCH_PAGES) -> list[list[tuple[int, str]]]:
    """按字符预算分批。单页超预算时让它自己单独成批，宁可长也不截断。"""
    batches: list[list[tuple[int, str]]] = []
    current: list[tuple[int, str]] = []
    current_chars = 0

    for pg, text in pages:
        if current and (current_chars + len(text) > max_chars
                        or len(current) >= max_pages):
            batches.append(current)
            current, current_chars = [], 0
        current.append((pg, text))
        current_chars += len(text)

    if current:
        batches.append(current)
    return batches


def build_prompt(combined: str) -> str:
    return f"""Translate the following assessment manual appendix content to Chinese.
Rules:
- Translate all English text, headers, labels, footnotes
- Preserve ALL numbers, scores, percentages, statistical values exactly as-is
- Preserve table structure (keep pipes, dashes, alignment)
- Keep the page markers (--- Page N ---)

Content:
{combined}"""


def call_model(ollama: Client, prompt: str) -> str | None:
    """调模型，三次都失败返回 None。"""
    for attempt in range(3):
        try:
            resp = ollama.chat(
                model=MODEL,
                messages=[{"role": "user", "content": prompt}],
                options={"temperature": 0.1, "num_ctx": NUM_CTX},
                keep_alive="5m",
            )
            return resp["message"]["content"]
        except Exception as e:
            if attempt < 2:
                print(f"    retry {attempt + 1}: {e}")
                time.sleep(30)
            else:
                print(f"    ❌ {e}")
    return None


def parse_pages(response: str) -> dict[int, str]:
    """按页标记切分模型输出。模型改写或漏掉标记时，这一页不会出现在结果里，
    交给调用方的完整性校验兜底。"""
    parts = re.split(r"---\s*Page\s+(\d+)\s*---", response)
    pages: dict[int, str] = {}
    for i in range(1, len(parts) - 1, 2):
        body = parts[i + 1].strip()
        if body:
            pages[int(parts[i])] = body
    return pages


def write_page(output_dir: str, pdf_name: str, pg: int, body: str,
               translated: bool = True):
    path = os.path.join(output_dir, f"page-{pg:04d}.md")
    with open(path, "w") as f:
        f.write("---\n")
        f.write(f"source: {pdf_name}\n")
        f.write(f"page: {pg}\n")
        f.write(f"model: {MODEL}\n")
        f.write(f"status: {'translated' if translated else 'untranslated'}\n")
        f.write(f"translated: {datetime.now().strftime('%Y-%m-%d')}\n")
        f.write("---\n\n")
        f.write(f"# Page {pg}\n\n")
        if not translated:
            f.write("> ⚠️ 本页翻译失败，以下为英文原文。\n\n")
        f.write(body)


def translate_batch(ollama: Client, batch: list[tuple[int, str]],
                    output_dir: str, pdf_name: str) -> list[int]:
    """翻译一批页面，返回仍然缺失的页码。"""
    combined = "\n\n".join(f"--- Page {pg} ---\n{text}" for pg, text in batch)
    response = call_model(ollama, build_prompt(combined))
    if response is None:
        return [pg for pg, _ in batch]

    parsed = parse_pages(response)
    missing = []
    for pg, _ in batch:
        if pg in parsed:
            write_page(output_dir, pdf_name, pg, parsed[pg])
        else:
            missing.append(pg)
    return missing


def main():
    if len(sys.argv) < 2:
        print("Usage: python3 translate_tables.py <pdf_path> [output_dir]")
        sys.exit(1)

    pdf_path = sys.argv[1]
    pdf_name = Path(pdf_path).name
    book_name = Path(pdf_path).stem
    output_dir = sys.argv[2] if len(sys.argv) > 2 else os.path.join(
        os.path.expanduser("~/note/Book Notes"),
        book_name
    )
    os.makedirs(output_dir, exist_ok=True)

    ollama = Client(host="http://localhost:11434", timeout=600)
    doc = fitz.open(pdf_path)
    total = len(doc)

    pages = []
    for pg in range(total):
        text = doc[pg].get_text().strip()
        if text:
            pages.append((pg + 1, text))
    doc.close()

    batches = build_batches(pages)
    print(f"Translating {len(pages)}/{total} non-empty pages with {MODEL}...")
    print(f"Output: {output_dir}")
    print(f"{len(batches)} batches (≤{MAX_BATCH_CHARS:,} chars / ≤{MAX_BATCH_PAGES} pages each)")

    failed: list[int] = []
    for bi, batch in enumerate(batches):
        span = f"{batch[0][0]}-{batch[-1][0]}" if len(batch) > 1 else str(batch[0][0])
        size = sum(len(t) for _, t in batch)
        print(f"  [{bi + 1}/{len(batches)}] pages {span} ({size:,} chars)", flush=True)
        if len(batch) == 1 and size > SOLO_PAGE_WARN_CHARS:
            print(f"    ⚠️  单页 {size:,} 字符，可能超出 num_ctx={NUM_CTX}")

        missing = translate_batch(ollama, batch, output_dir, pdf_name)

        # 整批翻译时模型可能改写或漏掉页标记，缺哪页就单独重翻哪页
        for pg in missing:
            text = dict(batch)[pg]
            print(f"    ↻ page {pg} 缺失，单独重翻", flush=True)
            response = call_model(ollama, build_prompt(f"--- Page {pg} ---\n{text}"))
            if response:
                # 单页时直接用已知页码落盘，不依赖模型保留页标记
                parsed = parse_pages(response)
                write_page(output_dir, pdf_name, pg, parsed.get(pg, response.strip()))
            else:
                # 仍然失败：至少把英文原文留下，并标记未翻译
                write_page(output_dir, pdf_name, pg, text, translated=False)
                failed.append(pg)

    # 生成索引（用书名而非 _index.md，避免 Obsidian 图谱重名）
    safe_name = re.sub(r'[\\/:*?"<>|]', "", book_name)
    written = sorted(Path(output_dir).glob("page-*.md"))
    with open(os.path.join(output_dir, f"_{safe_name}.md"), "w") as f:
        f.write(f"# {book_name}\n\n")
        f.write(f"Translated with {MODEL}\n\n")
        for p in written:
            num = re.search(r"page-(\d+)", p.name).group(1)
            f.write(f"- [Page {int(num)}]({p.name})\n")

    # 完整性校验：非空页都得有对应文件
    expected = {pg for pg, _ in pages}
    produced = {int(re.search(r"page-(\d+)", p.name).group(1)) for p in written}
    lost = sorted(expected - produced)

    print(f"\nDone. {len(produced)}/{len(expected)} pages → {output_dir}/")
    if failed:
        print(f"⚠️  {len(failed)} 页翻译失败，已写入英文原文: {failed}")
    if lost:
        print(f"❌ {len(lost)} 页没有落文件: {lost}")
        sys.exit(1)


if __name__ == "__main__":
    main()
