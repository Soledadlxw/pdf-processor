#!/usr/bin/env python3
"""engine.py 的图表/表格分支测试：table_extraction 必须能独立于 render_charts 生效。

用假的 Ollama 客户端和假的表格抽取器跑通 process_book，不需要真模型。
"""

import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import fitz  # noqa: E402

import engine  # noqa: E402

BODY = "Assessment of adaptive behavior across domains and subdomains. " * 40
TABLE_MD = "| 年龄 | 标准分 |\n|:---:|:---:|\n| 5 | 87 |\n"


class _FakeResponse(dict):
    pass


class _FakeClient:
    """够用的 ollama.Client 替身。"""

    def __init__(self, *args, **kwargs):
        self.calls = []

    def chat(self, model=None, messages=None, **kwargs):
        self.calls.append(model)
        return {"message": {"content": "# 提取结果\n\n## TL;DR\n\n测试正文。"}}


def _build_pdf(path: Path, text_pages: int = 3, sparse_pages: int = 2):
    """文字页 + 稀疏页（会被 classify_page 判成图表页）。"""
    doc = fitz.open()
    for _ in range(text_pages):
        page = doc.new_page(width=612, height=792)
        page.insert_textbox(fitz.Rect(72, 210, 540, 700), BODY, fontsize=9)
    for i in range(sparse_pages):
        page = doc.new_page(width=612, height=792)
        page.insert_text((72, 300), f"Table {i + 1}", fontsize=9)
    doc.save(str(path))
    doc.close()


def _run(tmp: Path, table_extraction: bool, render_charts: bool) -> str:
    """跑一次 process_book（fast 模式，只走 Stage 1），返回生成的笔记内容。"""
    pdf = tmp / "manual.pdf"
    _build_pdf(pdf)

    prompt = tmp / "stage1.txt"
    prompt.write_text("Summarize:\n{chapter_text}")

    config = {
        "extract_model": "fake-model",
        "stage1_prompt": str(prompt),
        "max_chars_per_chapter": 500_000,
        "max_retries": 1,
        "table_extraction": table_extraction,
        "render_charts": render_charts,
        "output_dir": "Book Notes",
        "attachments_dir": "_attachments",
    }
    book_meta = {"cite_key": "TEST", "title": "Manual", "title_zh": "测试手册"}

    real_client, real_table = engine.Client, engine.extract_table_markdown
    real_home = os.environ.get("HOME")
    engine.Client = _FakeClient
    engine.extract_table_markdown = lambda pdf_path, page: TABLE_MD
    os.environ["HOME"] = str(tmp)
    try:
        engine.process_book(str(pdf), book_meta, config, fast_mode=True)
    finally:
        engine.Client, engine.extract_table_markdown = real_client, real_table
        if real_home is not None:
            os.environ["HOME"] = real_home

    notes = [p for p in (tmp / "note" / "Book Notes" / "测试手册").glob("*.md")
             if not p.name.startswith("_")]
    assert notes, "应该生成章节笔记"
    return notes[0].read_text()


def test_tables_are_extracted_without_render_charts():
    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        note = _run(tmp, table_extraction=True, render_charts=False)

        assert TABLE_MD.strip() in note, "只开 table_extraction 时表格必须进笔记"
        assert not (tmp / "note" / "_attachments").exists(), "不该渲染 PNG"


def test_nothing_emitted_when_both_disabled():
    with tempfile.TemporaryDirectory() as d:
        note = _run(Path(d), table_extraction=False, render_charts=False)

        assert "本章图表" not in note
        assert TABLE_MD.strip() not in note


def test_charts_still_work_on_their_own():
    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        note = _run(tmp, table_extraction=False, render_charts=True)

        assert "![[" in note, "只开 render_charts 时应该嵌 PNG 链接"
        assert TABLE_MD.strip() not in note
        pngs = list((tmp / "note" / "_attachments").rglob("*.png"))
        assert pngs, "应该渲染出 PNG"


if __name__ == "__main__":
    failures = 0
    for name, fn in sorted(globals().items()):
        if not name.startswith("test_") or not callable(fn):
            continue
        try:
            fn()
            print(f"  PASS  {name}")
        except AssertionError as exc:
            failures += 1
            print(f"  FAIL  {name}: {exc}")
    print(f"\n{'FAILED' if failures else 'OK'} ({failures} failed)")
    sys.exit(1 if failures else 0)
