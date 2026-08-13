#!/usr/bin/env python3
"""cleaner / chapter_detect 的清洗与章节检测测试。

可以用 pytest 跑，也可以直接 `python3 tests/test_cleaner.py`（不依赖 pytest）。
"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import fitz  # noqa: E402

import cleaner  # noqa: E402
from cleaner import clean_book_pages, split_chapter_pages  # noqa: E402
from chapter_detect import detect_chapters  # noqa: E402
from engine import chunk_text_by_paragraphs  # noqa: E402

BODY = "Clinical manifestations of the disorder vary widely across individuals. " * 30
RUNNING_HEADER = "NELSON TEXTBOOK OF PEDIATRICS"
BANNER = "PART XII GROWTH DEVELOPMENT AND BEHAVIOR"


def _build_pdf(n_pages: int, banner: str | None = None,
               subheading_pages: tuple[int, ...] = ()) -> fitz.Document:
    """造一份带页眉、页码、可选跑版横幅和小标题的 PDF。

    版面位置（612x792）：
      y=20   顶部 2.5%  → 落在页眉带内，靠 header_ratio 裁掉
      y=95   12.0%      → 逃过页眉带，只能靠重复行检测拿下
      y=145  18.3%      → 小标题，正文区
      y=210+ 正文
      y=775  97.9%      → 落在页脚带内，纯页码
    """
    doc = fitz.open()
    for i in range(n_pages):
        page = doc.new_page(width=612, height=792)
        page.insert_text((72, 20), RUNNING_HEADER)
        page.insert_text((300, 775), str(600 + i))
        if banner:
            page.insert_text((72, 95), banner)
        if i in subheading_pages:
            page.insert_text((72, 145), "Treatment")
        page.insert_textbox(fitz.Rect(72, 210, 540, 700), BODY, fontsize=9)
    return doc


def _lines(text: str) -> list[str]:
    return [l.strip() for l in text.split("\n") if l.strip()]


# ── 惰性取文：TOC 可用时不该付全书提取的钱 ──────────────────────────

def test_detect_chapters_skips_text_extraction_when_toc_usable():
    doc = _build_pdf(20)
    doc.set_toc([[1, f"Chapter {i + 1}", p] for i, p in enumerate([1, 6, 11, 16])])

    calls = []

    def factory():
        calls.append(1)
        return ""

    chapters = detect_chapters(doc, text_factory=factory)

    assert len(chapters) >= 4, chapters
    assert chapters[0]["method"].startswith("toc")
    assert calls == [], "TOC 可用时不应该触发全书取文"


def test_detect_chapters_uses_text_factory_when_toc_missing():
    doc = _build_pdf(20)  # 没有 TOC

    calls = []

    def factory():
        calls.append(1)
        return "some book text"

    detect_chapters(doc, text_factory=factory)

    assert calls == [1], "没有 TOC 时应该恰好取一次全书文本"


def test_detect_chapters_still_accepts_positional_text():
    doc = _build_pdf(20)
    assert detect_chapters(doc, "some book text")  # 旧签名不能破


# ── 表格识别：必须显式打开 ──────────────────────────────────────────

def test_clean_book_pages_skips_table_extraction_by_default():
    doc = _build_pdf(3)
    calls = []
    original = cleaner.extract_tables_from_page
    cleaner.extract_tables_from_page = lambda *a, **k: (calls.append(1), [])[1]
    try:
        _, tables = clean_book_pages(doc, cite_key="TEST")
        assert calls == [], "cite_key 不该再隐式打开 find_tables"
        assert tables == []

        clean_book_pages(doc, cite_key="TEST", extract_tables=True)
        assert len(calls) == 3, "显式打开时每页都要抽一次"
    finally:
        cleaner.extract_tables_from_page = original


# ── 入模文本必须是清洗过的 ──────────────────────────────────────────

def test_split_chapter_pages_strips_header_footer_and_page_numbers():
    doc = _build_pdf(8)
    text, _ = split_chapter_pages(doc, 0, 7)

    assert RUNNING_HEADER not in text, "页眉带内的内容应被裁掉"
    assert not [l for l in _lines(text) if l.isdigit()], "不该留下纯页码行"
    assert "Clinical manifestations" in text, "正文不能被误删"


def test_split_chapter_pages_drops_banner_repeated_on_most_pages():
    doc = _build_pdf(8, banner=BANNER)
    text, _ = split_chapter_pages(doc, 0, 7)

    assert BANNER not in text, "逃出页眉带、几乎每页都有的短行应判为跑版页眉"
    assert "Clinical manifestations" in text


def test_split_chapter_pages_keeps_subheading_repeated_on_few_pages():
    doc = _build_pdf(8, subheading_pages=(2, 5))
    text, _ = split_chapter_pages(doc, 0, 7)

    assert "Treatment" in text, "只在少数页面重复的小标题必须保留"


def test_split_chapter_pages_respects_ratio_config():
    doc = _build_pdf(8, banner=BANNER)
    # 把页眉带放宽到 20%，y=95 的横幅就该被位置规则直接裁掉
    text, _ = split_chapter_pages(doc, 0, 7, header_ratio=0.2)

    assert BANNER not in text
    assert "Clinical manifestations" in text


def test_split_chapter_pages_separates_paragraphs():
    doc = _build_pdf(4)
    text, _ = split_chapter_pages(doc, 0, 3)

    assert "\n\n" in text, "段落之间要留空行，否则按段落分块会失效"


# ── 分块：max_chars 必须真的拦得住 ──────────────────────────────────

def test_chunk_text_by_paragraphs_hard_splits_oversized_paragraph():
    text = "x" * 50_000  # 一个段落，没有任何空行
    chunks = chunk_text_by_paragraphs(text, 20_000)

    assert len(chunks) >= 3, "单个超长段落必须被硬切"
    assert all(len(c) <= 20_000 for c in chunks), [len(c) for c in chunks]
    assert "".join(chunks) == text, "硬切不能丢内容"


def test_chunk_text_by_paragraphs_keeps_paragraphs_together():
    text = "\n\n".join(["p" * 400] * 10)
    chunks = chunk_text_by_paragraphs(text, 1000)

    assert len(chunks) > 1
    assert all(len(c) <= 1000 for c in chunks), [len(c) for c in chunks]


# ── 真实 PDF 回归 ───────────────────────────────────────────────────

def _fixture_pdf() -> Path | None:
    return next(iter(sorted((ROOT / "pdfs").glob("*.pdf"))), None)


def test_real_pdf_chapter_text_has_no_page_numbers_and_chunks():
    pdf = _fixture_pdf()
    if pdf is None:
        return  # 没有 fixture 就跳过

    doc = fitz.open(str(pdf))
    text, _ = split_chapter_pages(doc, 0, len(doc) - 1)

    # 判定粒度是「块」而不是「行」：页码是独立成块的，而表格里的行号
    # （0 Fetch FIFO / 1 Decode FIFO ...）是正文的一部分，不能删。
    paragraphs = [p.strip() for p in text.split("\n\n") if p.strip()]
    assert not [p for p in paragraphs if p.isdigit()], "仍有独立成块的页码"

    chunks = chunk_text_by_paragraphs(text, 25_000)
    assert all(len(c) <= 25_000 for c in chunks), [len(c) for c in chunks]
    assert len(chunks) > 1, "整章文本应该能被切成多块"
    doc.close()


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
