#!/usr/bin/env python3
"""engine.py — 统一书籍处理引擎（医学/历史/半导体/论文）

用法:
    python3 engine.py --config configs/medical.yaml
    python3 engine.py --config configs/medical.yaml --book "ESDM"
    python3 engine.py --config configs/medical.yaml --chapters "3,5"
    python3 engine.py --config configs/medical.yaml --fast
    python3 engine.py --config configs/medical.yaml --dry-run
    python3 engine.py --config configs/medical.yaml --init
    python3 engine.py --config configs/medical.yaml --bilingual
    python3 engine.py --config configs/medical.yaml --retry-failed
    python3 engine.py --config configs/medical.yaml --resume
"""

import fitz  # PyMuPDF
import urllib.request
import urllib.error
import json as json_lib
import importlib.util
from ollama import Client
import os
import sys
import time
import yaml
import re as re_mod
from pathlib import Path
from datetime import datetime

from cleaner import (clean_book_pages, extract_metadata,
                      classify_page, render_page_as_png,
                      describe_image, split_chapter_pages,
                      extract_table_markdown)
from chapter_detect import detect_chapters
from notes import write_children

CURRENT_YEAR = 2026
DSM5_TR_YEAR = 2022


def load_config(config_path: str) -> dict:
    with open(config_path) as f:
        config = yaml.safe_load(f) or {}
    config["_path"] = os.path.abspath(config_path)
    config["_dir"] = os.path.dirname(os.path.abspath(config_path))
    # 相对路径转绝对路径（基于 config 文件所在目录）
    for key in ["book_dir", "meta_file", "stage1_prompt", "stage2_prompt"]:
        val = config.get(key, "")
        if val and not os.path.isabs(val):
            config[key] = os.path.normpath(os.path.join(config["_dir"], val))
    return config


def load_prompt(path: str) -> str:
    if not os.path.exists(path):
        # 相对于 config 文件的目录
        return ""
    with open(path) as f:
        return f.read()


def slugify(text: str) -> str:
    text = re_mod.sub(r"[^\w\s-]", "", text)
    text = re_mod.sub(r"[-\s]+", "-", text)
    return text.strip("-")[:80]


def chunk_text_by_paragraphs(text: str, max_chars: int) -> list[str]:
    if max_chars <= 0:
        return [text] if text.strip() else []

    paragraphs = text.split("\n\n")
    chunks, current = [], ""
    for p in paragraphs:
        # 单个段落就超限时先硬切；否则它会独占一个块，max_chars 拦不住它
        while len(p) >= max_chars:
            if current.strip():
                chunks.append(current.strip())
                current = ""
            chunks.append(p[:max_chars].strip())
            p = p[max_chars:]
        if len(current) + len(p) < max_chars:
            current += p + "\n\n"
        else:
            if current.strip():
                chunks.append(current.strip())
            current = p + "\n\n"
    if current.strip():
        chunks.append(current.strip())
    return chunks


def assess_timeliness(year, dsm_version, chapter_title, rules, tags, output_dir, cite_key=""):
    warnings = []
    dsm_ver = str(dsm_version) if dsm_version else ""
    y = int(year) if year else 0
    age = CURRENT_YEAR - y if y else 0

    for rule in rules:
        try:
            check = rule["check"]
            ns = {
                "dsm_version": dsm_ver, "year": y, "age": age,
                "CURRENT_YEAR": CURRENT_YEAR, "tags": tags or [],
                "chapter_title": chapter_title,
            }
            if eval(check, {"__builtins__": {}}, ns):
                msg = rule.get("warning_zh", rule.get("warning", "")).format(
                    dsm_version=dsm_ver, year=y, age=age)
                warnings.append(msg)
        except Exception:
            pass
    return warnings


def unload_model(model_name: str):
    try:
        data = json_lib.dumps({"model": model_name, "keep_alive": 0}).encode()
        req = urllib.request.Request(
            "http://localhost:11434/api/generate",
            data=data, headers={"Content-Type": "application/json"})
        urllib.request.urlopen(req, timeout=5)
    except Exception:
        pass


# ═══════════════════════════════════════
# 主处理函数
# ═══════════════════════════════════════

def process_book(pdf_path: str, book_meta: dict, config: dict,
                 fast_mode: bool = False, dry_run: bool = False,
                 chapter_filter: set | None = None,
                 retry_failed: bool = False,
                 resume_mode: bool = False,
                 bilingual: bool = False):
    """处理一本书"""
    filename = Path(pdf_path).stem
    cite_key = book_meta.get("cite_key", filename[:6].upper())
    book_meta.setdefault("cite_key", cite_key)

    ollama = Client(host="http://localhost:11434",
                    timeout=config.get("http_timeout", 300))

    # 模型预热
    if config.get("model_warmup") and not dry_run:
        try:
            ollama.chat(model=config["extract_model"], messages=[
                {"role": "user", "content": "ready"}
            ], keep_alive="5m")
        except Exception:
            pass

    doc = fitz.open(pdf_path)
    book_title = book_meta.get("title", filename)
    print(f"\n{'='*60}")
    print(f"📖 {book_title}  [{cite_key}]")
    print(f"   {len(doc)} pages")

    # ── 清洗参数 ──
    header_ratio = config.get("header_y_ratio", 0.08)
    footer_ratio = config.get("footer_y_ratio", 0.08)
    trim_refs = config.get("trim_references", True)

    # ── 章节检测 ──
    chapters_override = book_meta.get("chapters_override")
    if chapters_override:
        chapters = chapters_override
        for c in chapters:
            c["start_page"] = c.get("start_page", 1) - 1
            c["end_page"] = c.get("end_page", 100) - 1
            c["method"] = "manual"
        print(f"  Using {len(chapters)} manual chapters from metadata")
    else:
        # 全书取文只有正则降级策略用得上，TOC 能用时不必付这笔开销
        chapters = detect_chapters(doc, text_factory=lambda: clean_book_pages(
            doc, header_ratio=header_ratio, footer_ratio=footer_ratio)[0])
        print(f"  Detected {len(chapters)} chapters (method: {chapters[0].get('method','?') if chapters else 'N/A'})")

    if dry_run:
        print(f"\n  {'─'*50}")
        total_pages = len(doc)
        est_total = 0
        for i, ch in enumerate(chapters):
            sp = ch.get('start_page', -1) + 1
            ep = ch.get('end_page', -1) + 1
            pp = ep - sp + 1 if ep > sp else 0
            est = pp * 1.5  # ~1.5 min/page for full pipeline
            est_total += est
            flag = " ⚠️ 大章" if pp > 50 else ""
            print(f"  {i+1:02d}. {ch['title'][:60]}  [pp. {sp}–{ep}]  ~{est:.0f}min{flag}")
        print(f"  {'─'*50}")
        print(f"  预估总耗时: {est_total:.0f}-{est_total*1.3:.0f} 分钟")
        doc.close()
        return chapters

    # ── 输出目录 ──
    book_title_zh = book_meta.get("title_zh", book_title)
    safe_book_name = slugify(book_title_zh) or slugify(filename)
    output_dir = os.path.join(
        os.path.expanduser("~/note"),
        config.get("output_dir", "Book Notes"),
        safe_book_name,
    )
    os.makedirs(output_dir, exist_ok=True)

    # ── retry-failed: 检测空壳 ──
    failed_set = set()
    if retry_failed:
        for f in Path(output_dir).glob("*.md"):
            if f.stat().st_size < 2000:
                failed_set.add(f.name)
        if failed_set:
            print(f"  Retrying {len(failed_set)} failed chapters: {failed_set}")

    # ── 加载 Prompt 模板 ──
    stage1_template = load_prompt(config.get("stage1_prompt", ""))
    stage2_template = load_prompt(config.get("stage2_prompt", ""))

    # ── 逐章处理 ──
    year = book_meta.get("year", 0) or 0
    dsm_ver = book_meta.get("dsm_version", "")
    attachments_dir = os.path.join(
        os.path.expanduser("~/note"),
        config.get("attachments_dir", "_attachments"),
        safe_book_name,
    )

    chapter_infos = []
    for i, ch in enumerate(chapters):
        ch_num = i + 1
        if chapter_filter and ch_num not in chapter_filter:
            continue

        pages_str = f"{ch.get('start_page',-1)+1}–{ch.get('end_page',-1)+1}"

        # retry-failed: 只处理空壳章
        ch_slug = slugify(ch['title'])
        note_filename = f"{ch_num:02d}-{ch_slug}.md"
        note_path = os.path.join(output_dir, note_filename)
        if retry_failed and failed_set and note_filename not in failed_set:
            continue

        # resume: 跳过已完成的章节（文件存在且 > 2000 字节）
        if resume_mode and os.path.exists(note_path) and os.path.getsize(note_path) > 2000:
            print(f"\n  [{ch_num}/{len(chapters)}] {ch['title'][:60]}  ⏭️  (already done)")
            continue

        print(f"\n  [{ch_num}/{len(chapters)}] {ch['title'][:60]}  ({pages_str})")

        # ── 文字/图表分流 ──
        start_pg = max(0, ch.get("start_page", 0))
        end_pg = min(ch.get("end_page", len(doc)-1), len(doc)-1)
        if ch.get("start_page", -1) < 0:
            per_ch = len(doc) // len(chapters)
            start_pg = i * per_ch
            end_pg = (i+1)*per_ch-1 if i+1 < len(chapters) else len(doc)-1

        chapter_text, image_parts = split_chapter_pages(
            doc, start_pg, end_pg,
            header_ratio=header_ratio, footer_ratio=footer_ratio,
            trim_refs=trim_refs,
        )
        if not chapter_text.strip() and not image_parts:
            print(f"    ⚠️  Empty chapter, skipping")
            continue

        print(f"    {len(chapter_text):,} text chars, {len(image_parts)} image pages")

        # ── Stage 1: 提取 ──
        max_cc = config.get("max_chars_per_chapter", 25000)
        extract_model = config["extract_model"]

        if len(chapter_text) > max_cc:
            sub_chunks = chunk_text_by_paragraphs(chapter_text, max_cc)
            sub_notes = []
            for j, sub in enumerate(sub_chunks):
                t0 = time.time()
                print(f"    Stage1 sub {j+1}/{len(sub_chunks)}...", end=" ", flush=True)
                prompt = stage1_template.format(
                    book_title=book_title, year=year,
                    edition=book_meta.get("edition", ""),
                    cite_key=cite_key,
                    authors=", ".join(book_meta.get("authors", [])),
                    chapter_title=ch["title"], pages=pages_str,
                    dsm_version=dsm_ver,
                    book_type=book_meta.get("book_type", ""),
                    chapter=ch_num, chapter_text=sub,
                )
                for attempt in range(config.get("max_retries", 3)):
                    try:
                        resp = ollama.chat(model=extract_model, messages=[
                            {"role": "user", "content": prompt}
                        ], options={"temperature": 0.1, "num_ctx": 16384},
                            keep_alive=config.get("keep_alive", "0s"))
                        sub_notes.append(resp["message"]["content"])
                        break
                    except Exception as e:
                        print(f"retry{attempt+1}:{e}", end=" ")
                        time.sleep(30)
                else:
                    sub_notes.append(f"[Stage 1 failed: section {j+1}]")
                print(f"{time.time()-t0:.0f}s")
            english_summary = "\n\n## ---\n\n".join(sub_notes)
        else:
            t0 = time.time()
            print(f"    Stage1...", end=" ", flush=True)
            prompt = stage1_template.format(
                book_title=book_title, year=year,
                edition=book_meta.get("edition", ""),
                cite_key=cite_key,
                authors=", ".join(book_meta.get("authors", [])),
                chapter_title=ch["title"], pages=pages_str,
                dsm_version=dsm_ver,
                book_type=book_meta.get("book_type", ""),
                chapter=ch_num, chapter_text=chapter_text,
            )
            for attempt in range(config.get("max_retries", 3)):
                try:
                    resp = ollama.chat(model=extract_model, messages=[
                        {"role": "user", "content": prompt}
                    ], options={"temperature": 0.1, "num_ctx": 16384},
                        keep_alive=config.get("keep_alive", "0s"))
                    english_summary = resp["message"]["content"]
                    break
                except Exception as e:
                    print(f"retry{attempt+1}:{e}", end=" ")
                    time.sleep(30)
            else:
                english_summary = f"[Stage 1 failed: {ch['title']}]"
            print(f"{time.time()-t0:.0f}s")

        if fast_mode:
            final_note = english_summary
            mode = "fast"
        elif bilingual:
            # 双语对照：Stage 2 保留原文
            unload_model(extract_model)
            time.sleep(5)
            t0 = time.time()
            print(f"    Stage2 (bilingual)...", end=" ", flush=True)
            bilingual_prompt = stage2_template.replace(
                "中文临床笔记（Markdown，不要用代码块包裹）：",
                "中英分段对照笔记，每段先保留英文原文（> EN:），再接中文翻译（> ZH:）："
            ) if stage2_template else ""
            prompt = bilingual_prompt.format(
                book_zh=book_title_zh, book_title=book_title,
                year=year, edition=book_meta.get("edition", ""),
                cite_key=cite_key, chapter_title=ch["title"],
                dsm_version=dsm_ver,
                english_summary=english_summary,
            )
            try:
                resp = ollama.chat(model=config["translate_model"], messages=[
                    {"role": "user", "content": prompt}
                ], options={"temperature": 0.2, "num_ctx": 16384},
                    keep_alive=config.get("keep_alive", "0s"))
                final_note = resp["message"]["content"]
            except Exception:
                final_note = english_summary
            mode = "bilingual"
            unload_model(config["translate_model"])
        else:
            # Stage 2: 翻译
            unload_model(extract_model)
            time.sleep(5)
            t0 = time.time()
            print(f"    Stage2...", end=" ", flush=True)
            prompt = stage2_template.format(
                book_zh=book_title_zh, book_title=book_title,
                year=year, edition=book_meta.get("edition", ""),
                cite_key=cite_key, chapter_title=ch["title"],
                dsm_version=dsm_ver,
                english_summary=english_summary,
            )
            for attempt in range(config.get("max_retries", 3)):
                try:
                    resp = ollama.chat(model=config["translate_model"], messages=[
                        {"role": "user", "content": prompt}
                    ], options={"temperature": 0.2, "num_ctx": 16384},
                        keep_alive=config.get("keep_alive", "0s"))
                    final_note = resp["message"]["content"]
                    break
                except Exception as e:
                    print(f"retry{attempt+1}:{e}", end=" ")
                    time.sleep(30)
            else:
                final_note = english_summary
            mode = "full"
            unload_model(config["translate_model"])
            print(f"{time.time()-t0:.0f}s")

        # ── 时效性 ──
        timeliness = assess_timeliness(
            year, dsm_ver, ch["title"],
            config.get("timeliness_rules", []),
            book_meta.get("tags", []),
            output_dir, cite_key,
        )

        # ── 图表渲染 + 表格提取 ──
        image_refs = ""
        attach_dir = os.path.join(
            os.path.expanduser("~/note"),
            config.get("attachments_dir", "_attachments"),
            safe_book_name,
        )
        # table_extraction 独立于 render_charts：只想要表格、不想要 PNG 是常见组合
        # （评估量表手册就是这样），原来它嵌在 render_charts 里面，单开等于没开。
        want_tables = bool(config.get("table_extraction"))
        want_charts = bool(config.get("render_charts"))
        if image_parts and (want_tables or want_charts):
            if want_charts:
                os.makedirs(attach_dir, exist_ok=True)
            for ip in image_parts:
                page_no = ip["page"]
                table_md = extract_table_markdown(pdf_path, page_no) if want_tables else None

                if not table_md and not want_charts:
                    continue  # 没抽到表格又不渲染图，这一页没什么可写的

                image_refs += f"\n### 图表 p.{page_no}\n\n"
                if table_md:
                    image_refs += table_md
                    print(f"    📊 p.{page_no}: table extracted")
                    continue

                png_path = os.path.join(attach_dir, ip["png_basename"])
                if not os.path.exists(png_path):
                    render_page_as_png(doc[page_no - 1], png_path,
                                       config.get("image_dpi", 200))
                rel = os.path.join(
                    os.path.basename(os.path.dirname(attach_dir)),
                    safe_book_name, ip["png_basename"])
                image_refs += f"![[{rel}]]\n\n"
                if config.get("gen_image_desc"):
                    desc = describe_image(png_path, ch["title"])
                    image_refs += f"> {desc}\n\n"
                print(f"    🖼️  p.{page_no}: PNG")

        # ── frontmatter ──
        fm = {
            "title": ch["title"],
            "book": book_title, "book_zh": book_title_zh,
            "cite_key": cite_key,
            "authors": book_meta.get("authors", []),
            "edition": book_meta.get("edition", ""),
            "year": year,
            "publisher": book_meta.get("publisher", ""),
            "chapter": ch_num, "pages": pages_str,
            "mode": mode,
            "stage1_model": config["extract_model"],
            "stage2_model": config.get("translate_model") if mode != "fast" else None,
            "processed": datetime.now().strftime("%Y-%m-%d"),
            "timeliness_warnings": timeliness,
            "tags": book_meta.get("tags", []) + ["to-process"],
        }
        if config.get("obsidian_aliases"):
            fm["aliases"] = [
                f"{cite_key} Ch{ch_num}",
                slugify(ch["title"][:20]),
            ]

        # ── 写笔记 ──
        yaml_fm = yaml.dump(fm, allow_unicode=True, default_flow_style=False).strip()
        with open(note_path, "w") as f:
            f.write(f"---\n{yaml_fm}\n---\n\n")
            f.write(f"# {ch['title']}\n\n")
            for w in timeliness:
                f.write(f"> {w}\n\n")

            if config.get("obsidian_breadcrumb"):
                prev_ch = next_ch = None
                if i > 0:
                    prev_ch = chapters[i-1]
                if i < len(chapters) - 1:
                    next_ch = chapters[i+1]
                nav = ""
                if prev_ch:
                    p_title = prev_ch.get('short_title') or prev_ch['title']
                    p_slug = slugify(prev_ch.get('title_zh') or prev_ch['title'])
                    nav += f"← [[{i:02d}-{p_slug}|{p_title}]]  "
                if next_ch:
                    n_title = next_ch.get('short_title') or next_ch['title']
                    n_slug = slugify(next_ch.get('title_zh') or next_ch['title'])
                    nav += f"|  [[{i+2:02d}-{n_slug}|{n_title}]] →"
                if nav:
                    f.write(f"{nav}\n\n")

            f.write(final_note)
            if image_refs:
                f.write(f"\n---\n## 📊 本章图表\n{image_refs}")

        print(f"    ✅ → {note_filename}")

        # ── 父子文档 ──
        child_paths = write_children(
            final_note, fm, Path(note_path),
            config.get("child_chunk_size", 4000),
        )
        if child_paths:
            print(f"    📦 {len(child_paths)} child chunks")

        chapter_infos.append({
            "num": ch_num, "title": ch["title"], "pages": pages_str,
            "timeliness": timeliness, "note_file": note_filename,
            "children": len(child_paths),
        })

    # ── _index.md ──
    write_book_index(output_dir, book_meta, chapter_infos, safe_book_name, cite_key)

    # 全书处理完后统一卸载模型
    unload_model(config.get("extract_model", ""))
    unload_model(config.get("translate_model", ""))

    doc.close()
    print(f"\n  📂 {output_dir}/")
    return chapter_infos


def write_book_index(output_dir, book_meta, chapters, safe_name, cite_key):
    """生成 _{书名}.md — 中文唯一，避免 Obsidian 图谱重名"""
    from pathlib import Path as PathLib
    import re as re_mod

    book_zh = book_meta.get("title_zh", safe_name)
    safe_zh = re_mod.sub(r'[\\/:*?"<>|]', '', book_zh)
    index_path = os.path.join(output_dir, f"_{safe_zh}.md")
    book_title = book_meta.get("title", safe_name)
    book_zh = book_meta.get("title_zh", book_title)

    # 先从目录扫描所有已有 .md 文件，合并章节信息
    existing_chapters = {}  # {num: {title, pages, note_file}}
    for f in sorted(PathLib(output_dir).glob("*.md")):
        if f.name.startswith("_"):
            continue  # 跳过索引文件和图谱文件
        # 解析文件名: "NN-chapter-slug.md"
        m = re_mod.match(r'(\d+)-(.+)\.md', f.name)
        if m:
            num = int(m.group(1))
            title = m.group(2).replace('-', ' ')
            existing_chapters[num] = {
                'num': num,
                'title': title,
                'pages': '?',
                'note_file': f.name,
                'timeliness': set(),
            }

    # 用本次处理的章节信息更新（有更准确的 title/pages/timeliness）
    for ch in chapters:
        num = ch.get('num', 0)
        if num:
            existing_chapters[num] = ch

    # 按章节号排序
    sorted_chapters = sorted(existing_chapters.values(), key=lambda x: x['num'])

    lines = [
        f"---",
        f"title: \"{book_title}\"",
        f"book_zh: \"{book_zh}\"",
        f"type: book-index",
        f"cite_key: \"{cite_key}\"",
        f"processed: \"{datetime.now().strftime('%Y-%m-%d')}\"",
        f"---",
        f"",
        f"# {book_zh}",
        f"> **{book_title}**  |  {book_meta.get('year','')}",
    ]
    all_warns = set()
    for ch in sorted_chapters:
        for w in ch.get("timeliness", []):
            all_warns.add(w)
    if all_warns:
        lines.append("\n## ⚠️ 时效性提醒\n")
        for w in sorted(all_warns):
            lines.append(f"- {w}")
        lines.append("")
    lines.append("## 章节\n")
    lines.append("| # | 章节 | 页数 | 时效性 |")
    lines.append("|:---:|------|------|:---:|")
    for ch in sorted_chapters:
        time_mark = "⚠️" if ch.get("timeliness") else "✅"
        display = ch.get('short_title') or ch.get('title_zh') or ch['title']
        lines.append(f"| {ch['num']} | [{display}]({ch.get('note_file','')}) | {ch['pages']} | {time_mark} |")
    lines.append("")
    with open(index_path, "w") as f:
        f.write("\n".join(lines))


def generate_metadata(config):
    book_dir = config.get("book_dir", "./books")
    meta_file = config.get("meta_file", "./metadata.yaml")
    os.makedirs(book_dir, exist_ok=True)
    pdfs = sorted(Path(book_dir).glob("*.pdf"))
    if not pdfs:
        print(f"❌ No PDFs in {book_dir}/")
        return
    existing = {}
    if os.path.exists(meta_file):
        with open(meta_file) as f:
            existing = yaml.safe_load(f) or {}
    fields = config.get("metadata_fields", {})
    new_meta = {}
    for p in pdfs:
        fn = p.name
        if fn in existing:
            new_meta[fn] = existing[fn]
            continue
        try:
            doc = fitz.open(str(p))
            embed = extract_metadata(doc)
            doc.close()
        except Exception:
            embed = {}
        entry = {}
        for k, v in fields.items():
            if k == "title" and embed.get("title"):
                entry[k] = embed["title"]
            elif k == "authors" and embed.get("author"):
                entry[k] = [a.strip() for a in embed["author"].split(";") if a.strip()]
            elif v.get("required"):
                entry[k] = ""
            else:
                entry[k] = ""
        new_meta[fn] = entry
    os.makedirs(os.path.dirname(meta_file) or ".", exist_ok=True)
    with open(meta_file, "w") as f:
        yaml.dump(new_meta, f, allow_unicode=True, default_flow_style=False, sort_keys=False)
    print(f"✅ {meta_file} ({len(pdfs)} books)")


# ═══════════════════════════════════════
# 入口
# ═══════════════════════════════════════

if __name__ == "__main__":
    config_path = None
    for i, arg in enumerate(sys.argv):
        if arg == "--config" and i + 1 < len(sys.argv):
            config_path = sys.argv[i + 1]

    if not config_path:
        print("Usage: python3 engine.py --config configs/medical.yaml [opts]")
        sys.exit(1)

    config = load_config(config_path)
    dry_run = "--dry-run" in sys.argv
    fast_mode = "--fast" in sys.argv
    init_mode = "--init" in sys.argv
    retry_failed = "--retry-failed" in sys.argv
    resume_mode = "--resume" in sys.argv
    bilingual = "--bilingual" in sys.argv
    book_filter = None
    chapter_filter = set()

    for i, arg in enumerate(sys.argv):
        if arg == "--book" and i + 1 < len(sys.argv):
            book_filter = sys.argv[i + 1]
        if arg == "--chapters" and i + 1 < len(sys.argv):
            chapter_filter = set(int(x.strip()) for x in sys.argv[i+1].split(",") if x.strip().isdigit())

    if init_mode:
        generate_metadata(config)
        sys.exit(0)

    meta_file = config.get("meta_file", "./metadata.yaml")
    if not os.path.exists(meta_file):
        print(f"❌ {meta_file} not found. Run with --init first.")
        sys.exit(1)

    with open(meta_file) as f:
        meta = yaml.safe_load(f) or {}

    book_dir = config.get("book_dir", "./books")
    pdfs = sorted(Path(book_dir).glob("*.pdf"))
    if book_filter:
        pdfs = [p for p in pdfs if book_filter.lower() in p.name.lower()]
    if not pdfs:
        print(f"❌ No PDFs in {book_dir}/")
        sys.exit(1)

    print(f"{'='*60}")
    print(f"Config:      {config_path}")
    print(f"Books:       {len(pdfs)}")
    print(f"Stage 1:     {config['extract_model']}")
    print(f"Stage 2:     {config.get('translate_model','N/A') if not fast_mode else 'SKIPPED'}")
    print(f"Mode:         {'DRY-RUN' if dry_run else 'fast' if fast_mode else 'bilingual' if bilingual else 'full'}")
    print(f"Output:       ~/note/{config.get('output_dir','Book Notes')}/")
    if config.get("table_extraction"):
        if importlib.util.find_spec("camelot") is None:
            print("⚠️  table_extraction 已开启，但没装 camelot，表格会被静默跳过")
            print("    pip install 'camelot-py[base]'")
        else:
            print("Tables:      camelot")
    print(f"{'='*60}")

    for pdf_path in pdfs:
        fn = pdf_path.name
        if fn not in meta:
            print(f"\n⚠️  {fn} not in metadata, skipping")
            continue
        bm = meta[fn]
        if not bm.get("cite_key"):
            print(f"\n⚠️  {fn}: cite_key missing, skipping")
            continue
        try:
            process_book(str(pdf_path), bm, config,
                         fast_mode=fast_mode, dry_run=dry_run,
                         chapter_filter=chapter_filter,
                         retry_failed=retry_failed,
                         resume_mode=resume_mode,
                         bilingual=bilingual)
        except KeyboardInterrupt:
            print(f"\n⏹  Interrupted. Run again to resume.")
            break
        except Exception as e:
            print(f"\n❌ Error: {e}")
            import traceback
            traceback.print_exc()

    print(f"\n{'='*60}")
    print(f"Done.")
    if not dry_run:
        print(f"Notes:   ~/note/{config.get('output_dir','Book Notes')}/")
        post_mod = config.get("postprocess_module")
        if post_mod:
            print(f"Next:    python3 -m {post_mod}")
