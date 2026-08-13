#!/usr/bin/env python3
"""translate_tables.py — 纯表格书籍逐页翻译（仅 qwen，不调 gemma4）

用法: python3 translate_tables.py <pdf_path> <output_dir>
"""

import fitz
import sys
import os
import re
import yaml
import time
from pathlib import Path
from datetime import datetime
from ollama import Client

MODEL = "qwen3.6:35b-mlx"


def main():
    if len(sys.argv) < 2:
        print("Usage: python3 translate_tables.py <pdf_path> [output_dir]")
        sys.exit(1)

    pdf_path = sys.argv[1]
    output_dir = sys.argv[2] if len(sys.argv) > 2 else os.path.join(
        os.path.expanduser("~/note/Book Notes"),
        Path(pdf_path).stem
    )
    os.makedirs(output_dir, exist_ok=True)

    ollama = Client(host="http://localhost:11434", timeout=600)
    doc = fitz.open(pdf_path)
    total = len(doc)

    print(f"Translating {total} pages with {MODEL}...")
    print(f"Output: {output_dir}")

    # 分批处理，每 10 页合并一次提交
    BATCH = 10

    for start in range(0, total, BATCH):
        end = min(start + BATCH, total)
        batch_text = []

        for pg in range(start, end):
            page = doc[pg]
            text = page.get_text().strip()
            if text:
                batch_text.append(f"--- Page {pg+1} ---\n{text}")

        if not batch_text:
            continue

        combined = "\n\n".join(batch_text)

        # 只翻译英文文本，保留数字和表格格式
        prompt = f"""Translate the following assessment manual appendix content to Chinese.
Rules:
- Translate all English text, headers, labels, footnotes
- Preserve ALL numbers, scores, percentages, statistical values exactly as-is
- Preserve table structure (keep pipes, dashes, alignment)
- Keep the page markers (--- Page N ---)

Content:
{combined[:15000]}"""

        for attempt in range(3):
            try:
                resp = ollama.chat(model=MODEL, messages=[
                    {"role": "user", "content": prompt}
                ], options={"temperature": 0.1, "num_ctx": 16384}, keep_alive="5m")
                chinese = resp["message"]["content"]
                break
            except Exception as e:
                if attempt < 2:
                    print(f"  retry {attempt+1}: {e}")
                    time.sleep(30)
                else:
                    chinese = combined  # fallback to original
                    print(f"  ❌ Failed: {e}")

        # 写回分页文件
        pages_output = chinese.split("--- Page ")
        for part in pages_output:
            if not part.strip():
                continue
            m = re.match(r'(\d+)', part)
            if m:
                pgnum = int(m.group(1))
                fname = os.path.join(output_dir, f"page-{pgnum:04d}.md")
                with open(fname, "w") as f:
                    f.write(f"---\n")
                    f.write(f"source: {Path(pdf_path).name}\n")
                    f.write(f"page: {pgnum}\n")
                    f.write(f"translated: {datetime.now().strftime('%Y-%m-%d')}\n")
                    f.write(f"---\n\n")
                    f.write(f"# Page {pgnum}\n\n")
                    f.write(part.strip())

        print(f"  Pages {start+1}-{end}: ✅")

    doc.close()

    # 生成索引
    pages = sorted(Path(output_dir).glob("page-*.md"))
    with open(os.path.join(output_dir, "_index.md"), "w") as f:
        f.write(f"# {Path(pdf_path).stem}\n\n")
        f.write(f"Translated with {MODEL}\n\n")
        for p in pages:
            num = re.search(r'page-(\d+)', p.name).group(1)
            f.write(f"- [Page {num}]({p.name})\n")

    print(f"\nDone. {total} pages → {output_dir}/")


if __name__ == "__main__":
    main()
