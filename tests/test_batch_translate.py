#!/usr/bin/env python3
"""batch_translate.py 的子文档重建测试。

重点是两件事：子文档不再单独调 LLM（Phase 2 不做双倍翻译），以及子文档必须是
父笔记的逐字切片（父子文档检索的前提）。
"""

import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from batch_translate import (  # noqa: E402
    CHILD_CHUNK_SIZE,
    read_frontmatter,
    rebuild_children,
    split_sections,
    translate_note,
)

# 四个小节各约 1200 字符，共约 4800 —— 按 4000 的块大小切成 2 块，
# 其中第一块由 3 个小节拼成，正好用来验证「拼接后仍是父文本的逐字切片」
ZH_BODY = "\n".join(
    f"## 第{i}节\n\n" + f"这是第{i}节的中文内容，用于验证子文档切分。" * 57
    for i in (1, 2, 3, 4)
)

EN_NOTE = """---
book: Test Book
book_zh: 测试书
cite_key: TEST
chapter: 1
year: 2020
edition: 1
mode: fast
title: Overview of Development
---

# Overview of Development

## Key Points

English content that Phase 1 produced.

## Assessment

More English content.
"""


class _FakeClient:
    """按 prompt 内容分辨是在翻标题还是翻正文。"""

    def __init__(self, *args, **kwargs):
        self.calls = []

    def chat(self, model=None, messages=None, **kwargs):
        prompt = messages[0]["content"]
        self.calls.append(prompt)
        if "extract a 5-8 character short label" in prompt:
            return {"message": {"content": "title_zh: 发育概述\nshort_title: 发育概述"}}
        return {"message": {"content": ZH_BODY}}


def _setup(tmp: Path) -> tuple[Path, Path]:
    """造一篇 Phase 1 的英文笔记 + 一份英文子文档。"""
    note = tmp / "01-Overview-of-Development.md"
    note.write_text(EN_NOTE)

    chunks = tmp / "_chunks"
    chunks.mkdir()
    stale = chunks / "test-book-Overview-of-Development-1.md"
    stale.write_text(
        "---\nchild_of: 01-Overview-of-Development.md\nbook: Test Book\n---\n\n"
        "English chunk content left over from Phase 1.\n"
    )
    return note, stale


def _body_of(path: Path) -> str:
    text = path.read_text()
    return text[text.index("---", 3) + 3:].strip()


# ── 切分本身 ────────────────────────────────────────────────────────

def test_split_sections_produces_verbatim_slices():
    for chunk in split_sections(ZH_BODY, CHILD_CHUNK_SIZE):
        assert chunk in ZH_BODY, "子块必须是父文本的逐字切片"


def test_split_sections_respects_size_where_possible():
    chunks = split_sections(ZH_BODY, CHILD_CHUNK_SIZE)
    assert len(chunks) == 2, [len(c) for c in chunks]
    assert any("第1节" in c and "第3节" in c for c in chunks), "小节应尽量拼满一块"
    # 单个小节本身超限时只能自成一块，其余都要在预算内
    assert all(len(c) <= CHILD_CHUNK_SIZE or "\n## " not in c for c in chunks)


def test_split_sections_handles_text_without_headings():
    assert split_sections("没有任何小节标题的正文", 4000) == ["没有任何小节标题的正文"]


def test_split_sections_ignores_empty_input():
    assert split_sections("", 4000) == []


# ── 重建子文档 ──────────────────────────────────────────────────────

def test_rebuild_children_removes_stale_english_chunks():
    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        note, stale = _setup(tmp)
        new_parent = tmp / "01-发育概述.md"
        new_parent.write_text("---\nchapter: 1\n---\n\n" + ZH_BODY)

        rebuild_children(ZH_BODY, {"chapter": 1}, note.name, new_parent)

        assert not stale.exists(), "Phase 1 留下的英文子文档必须被清掉"


def test_rebuild_children_names_files_after_the_parent():
    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        note, _ = _setup(tmp)
        new_parent = tmp / "01-发育概述.md"

        written = rebuild_children(ZH_BODY, {"chapter": 1}, note.name, new_parent)

        # 带章号前缀，重名章节的子文档不会互相覆盖
        assert [p.name for p in written] == ["01-发育概述-1.md", "01-发育概述-2.md"]
        for p in written:
            assert read_frontmatter(p)["child_of"] == "01-发育概述.md"


# ── 端到端：翻一篇笔记 ──────────────────────────────────────────────

def test_translate_note_does_not_translate_children_again():
    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        note, stale = _setup(tmp)
        fake = _FakeClient()

        assert translate_note(str(note), fake) is True

        # 1 次翻标题 + 1 次翻正文，子文档不再各自调一次
        assert len(fake.calls) == 2, [c[:60] for c in fake.calls]
        assert not stale.exists()

        parent = tmp / "01-发育概述.md"
        assert parent.exists(), list(tmp.iterdir())
        children = sorted((tmp / "_chunks").glob("*.md"))
        assert len(children) == 2

        parent_body = _body_of(parent)
        for child in children:
            assert _body_of(child) in parent_body, "子文档必须能在父笔记里逐字找到"


def test_translate_note_marks_frontmatter():
    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        note, _ = _setup(tmp)
        translate_note(str(note), _FakeClient())

        fm = read_frontmatter(tmp / "01-发育概述.md")
        assert fm["mode"] == "full"
        assert fm["title_zh"] == "发育概述"
        assert fm["short_title"] == "发育概述"


def test_translate_note_skips_already_translated():
    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        note = Path(tmp) / "01-已翻译.md"
        note.write_text(
            "---\nmode: full\ntitle_zh: 已翻译\nchapter: 1\n---\n\n## 小节\n\n内容\n"
        )
        fake = _FakeClient()

        assert translate_note(str(note), fake) is False
        assert fake.calls == []


# ── 与旧实现的调用量对比 ────────────────────────────────────────────

def test_old_implementation_would_have_called_llm_per_chunk():
    """锁住这次改动的收益：旧实现每个子文档都要多调一次 LLM。"""
    source = (ROOT / "batch_translate.py").read_text()
    assert "cf_prompt" not in source and "cf_resp" not in source, \
        "子文档不应再单独送 LLM"
    # 只剩两个调用点：translate_title 翻标题、translate_note 翻正文
    assert source.count("ollama.chat") == 2, source.count("ollama.chat")


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
