#!/usr/bin/env python3
"""notes.py —— 父子文档切分的测试。

三条不变量：
  1. 每一块都是父文本的逐字切片
  2. 每一块都不超过 chunk_size（硬上限，不是「尽量」）
  3. 切完不丢内容
"""

import re
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from notes import (  # noqa: E402
    read_frontmatter,
    section_titles,
    split_sections,
    write_children,
)

SIZE = 1000


def _dense(n: int) -> str:
    """一段没有空行、没有子标题的连续文本。"""
    return "详细描述临床表现。" * n


def _norm(text: str) -> str:
    return re.sub(r"\s+", "", text)


def _check_invariants(text: str, chunks: list[str], chunk_size: int):
    for c in chunks:
        assert c in text, f"不是逐字切片: {c[:40]!r}"
        assert len(c) <= chunk_size, f"超出上限 {len(c)} > {chunk_size}"
    assert _norm("".join(chunks)) == _norm(text), "切分丢了内容"


# ── 基本切分 ────────────────────────────────────────────────────────

def test_splits_at_heading_boundaries():
    text = "\n".join(f"## 小节{i}\n\n{_dense(80)}" for i in range(1, 5))
    chunks = split_sections(text, SIZE)

    _check_invariants(text, chunks, SIZE)
    assert len(chunks) == 4, [len(c) for c in chunks]


def test_packs_small_sections_together():
    text = "\n".join(f"## 小节{i}\n\n短内容。" for i in range(1, 6))
    chunks = split_sections(text, SIZE)

    _check_invariants(text, chunks, SIZE)
    assert len(chunks) == 1, "小节都很短时应该攒成一块"


def test_empty_input():
    assert split_sections("", SIZE) == []
    assert split_sections("   \n\n  ", SIZE) == []


# ── 超长小节的二次切分（这次改动的重点）──────────────────────────────

def test_oversized_section_splits_at_h3():
    text = "## 大节\n\n" + "\n".join(f"### 子节{i}\n\n{_dense(80)}" for i in range(1, 5))
    chunks = split_sections(text, SIZE)

    _check_invariants(text, chunks, SIZE)
    assert len(chunks) == 4, [len(c) for c in chunks]
    assert all(c.lstrip().startswith("### ") for c in chunks[1:]), \
        "除首块外都应切在 ### 边界上"


def test_oversized_section_splits_at_paragraphs():
    text = "## 大节\n\n" + "\n\n".join(_dense(80) for _ in range(5))
    chunks = split_sections(text, SIZE)

    _check_invariants(text, chunks, SIZE)
    assert len(chunks) == 5, [len(c) for c in chunks]


def test_oversized_section_falls_back_to_hard_split():
    # 既没有子标题也没有空行 —— 只能硬切，但绝不能丢内容或超上限
    text = "## 大节\n\n" + _dense(500)
    chunks = split_sections(text, SIZE)

    _check_invariants(text, chunks, SIZE)
    assert len(chunks) >= 5


def test_text_without_any_heading_still_capped():
    text = _dense(500)
    chunks = split_sections(text, SIZE)

    _check_invariants(text, chunks, SIZE)
    assert len(chunks) > 1, "没有标题也不能整篇变成一块"


# ── 小节名写进 frontmatter ──────────────────────────────────────────

def test_section_titles_picks_up_h2_and_h3():
    assert section_titles("## 诊断标准\n\n内容\n\n### 补充\n\n内容") == ["诊断标准", "补充"]


def test_section_titles_empty_for_plain_text():
    assert section_titles("没有任何标题的正文") == []


# ── 写盘 ────────────────────────────────────────────────────────────

def _parent(tmp: Path, name: str = "01-测试章.md") -> Path:
    p = tmp / name
    p.write_text("---\nchapter: 1\n---\n\n正文")
    return p


def test_single_chunk_writes_no_children():
    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        parent = _parent(tmp)

        written = write_children("## 小节\n\n很短的内容。", {"chapter": 1}, parent, SIZE)

        assert written == [], "只有一块时不该写子文档（那是父笔记的复制品）"
        assert not (tmp / "_chunks").exists()


def test_children_are_named_after_parent_and_record_sections():
    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        parent = _parent(tmp)
        text = "\n".join(f"## 小节{i}\n\n{_dense(80)}" for i in range(1, 4))

        written = write_children(text, {"chapter": 1}, parent, SIZE)

        assert [p.name for p in written] == [
            "01-测试章-1.md", "01-测试章-2.md", "01-测试章-3.md"]
        first = read_frontmatter(written[0])
        assert first["child_of"] == "01-测试章.md"
        assert first["child_index"] == 1
        assert first["sections"] == ["小节1"]


def test_stale_children_are_removed_by_child_of():
    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        parent = _parent(tmp)
        chunks_dir = tmp / "_chunks"
        chunks_dir.mkdir()
        mine = chunks_dir / "old-english-1.md"
        mine.write_text("---\nchild_of: 01-Old-English.md\n---\n\nstale\n")
        other = chunks_dir / "别的章-1.md"
        other.write_text("---\nchild_of: 02-别的章.md\n---\n\n不该被删\n")

        text = "\n".join(f"## 小节{i}\n\n{_dense(80)}" for i in range(1, 4))
        write_children(text, {"chapter": 1}, parent, SIZE,
                       stale_names={"01-Old-English.md"})

        assert not mine.exists(), "旧子文档应按 child_of 清掉"
        assert other.exists(), "别的父笔记的子文档不能误删"


def test_children_bodies_are_verbatim_slices_of_parent():
    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        parent = _parent(tmp)
        text = "\n".join(f"## 小节{i}\n\n{_dense(80)}" for i in range(1, 4))

        for child in write_children(text, {"chapter": 1}, parent, SIZE):
            body = child.read_text().split("---", 2)[2].strip()
            assert body in text


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
