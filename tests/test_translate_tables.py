#!/usr/bin/env python3
"""translate_tables.py 的分批与落盘测试。

可以用 pytest 跑，也可以直接 `python3 tests/test_translate_tables.py`。
"""

import re
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import fitz  # noqa: E402

from translate_tables import (  # noqa: E402
    MAX_BATCH_CHARS,
    MAX_BATCH_PAGES,
    build_batches,
    parse_pages,
    write_page,
)


def _pages(sizes: list[int]) -> list[tuple[int, str]]:
    return [(i + 1, "x" * n) for i, n in enumerate(sizes)]


# ── 分批：不能再因为固定页数 + 截断而掉页 ──────────────────────────

def test_every_page_lands_in_exactly_one_batch():
    pages = _pages([5800] * 25)
    batches = build_batches(pages)

    seen = [pg for b in batches for pg, _ in b]
    assert seen == [pg for pg, _ in pages], "分批不能丢页也不能重复"


def test_batches_respect_char_budget():
    # 每页 5800 字符：旧逻辑固定 10 页一批 = 58,000 字符，截断到 15,000 后掉 7 页
    pages = _pages([5800] * 25)
    batches = build_batches(pages)

    for b in batches:
        size = sum(len(t) for _, t in b)
        assert len(b) == 1 or size <= MAX_BATCH_CHARS, f"{len(b)} 页 / {size} 字符"


def test_batches_respect_page_cap_for_small_pages():
    pages = _pages([100] * 50)
    batches = build_batches(pages)

    assert all(len(b) <= MAX_BATCH_PAGES for b in batches)


def test_oversized_page_gets_its_own_batch_and_is_not_truncated():
    pages = _pages([200, MAX_BATCH_CHARS * 3, 200])
    batches = build_batches(pages)

    big = [b for b in batches if any(len(t) > MAX_BATCH_CHARS for _, t in b)]
    assert len(big) == 1 and len(big[0]) == 1, "超大页应该自己单独成批"
    assert len(big[0][0][1]) == MAX_BATCH_CHARS * 3, "内容不能被截断"


def test_empty_input():
    assert build_batches([]) == []


# ── 解析模型输出 ────────────────────────────────────────────────────

def test_parse_pages_extracts_all_markers():
    resp = (
        "--- Page 1 ---\n第一页内容\n\n"
        "--- Page 2 ---\n第二页内容\n\n"
        "--- Page 3 ---\n第三页内容"
    )
    parsed = parse_pages(resp)

    assert sorted(parsed) == [1, 2, 3]
    assert parsed[2] == "第二页内容"
    assert "--- Page" not in parsed[1], "页标记不应残留在正文里"


def test_parse_pages_tolerates_preamble_and_spacing():
    resp = "好的，以下是翻译：\n\n---  Page  7  ---\n年龄当量表\n\n--- Page 8 ---\n常模"
    parsed = parse_pages(resp)

    assert sorted(parsed) == [7, 8]
    assert parsed[7] == "年龄当量表"


def test_parse_pages_reports_missing_pages():
    # 模型只吐了 3 页中的 2 页 —— 调用方要能发现第 2 页缺了
    parsed = parse_pages("--- Page 1 ---\na\n\n--- Page 3 ---\nc")
    missing = [pg for pg in (1, 2, 3) if pg not in parsed]

    assert missing == [2]


# ── 落盘 ────────────────────────────────────────────────────────────

def test_write_page_marks_untranslated_fallback():
    with tempfile.TemporaryDirectory() as d:
        write_page(d, "Vineland.pdf", 12, "raw english", translated=False)
        text = (Path(d) / "page-0012.md").read_text()

        assert "status: untranslated" in text
        assert "翻译失败" in text, "回退成英文原文时必须留下痕迹"
        assert "raw english" in text


def test_write_page_normal():
    with tempfile.TemporaryDirectory() as d:
        write_page(d, "Vineland.pdf", 3, "中文内容")
        text = (Path(d) / "page-0003.md").read_text()

        assert "status: translated" in text
        assert "page: 3" in text
        assert "中文内容" in text


# ── 索引文件命名 ────────────────────────────────────────────────────

def test_index_is_not_named_index_md():
    source = (ROOT / "translate_tables.py").read_text()
    assert '"_index.md"' not in source, "索引应命名为 _{书名}.md，避免 Obsidian 图谱重名"
    assert 'f"_{safe_name}.md"' in source


# ── 真实 PDF：旧逻辑会掉多少页 ──────────────────────────────────────

def test_real_pdf_no_page_is_dropped_by_batching():
    pdf = next(iter(sorted((ROOT / "pdfs").glob("*.pdf"))), None)
    if pdf is None:
        return

    doc = fitz.open(str(pdf))
    pages = [(p + 1, doc[p].get_text().strip())
             for p in range(len(doc)) if doc[p].get_text().strip()]
    doc.close()

    # 旧逻辑：固定 10 页一批，prompt 里只留前 15000 字符
    old_delivered = set()
    for start in range(0, len(pages), 10):
        combined = "\n\n".join(f"--- Page {pg} ---\n{t}"
                               for pg, t in pages[start:start + 10])
        for m in re.finditer(r"---\s*Page\s+(\d+)\s*---", combined[:15000]):
            old_delivered.add(int(m.group(1)))

    new_delivered = {pg for b in build_batches(pages) for pg, _ in b}
    expected = {pg for pg, _ in pages}

    assert new_delivered == expected, "新分批必须把每一页都送进模型"
    assert old_delivered < expected, "本 fixture 应能复现旧逻辑掉页（否则这个回归测试没意义）"


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
