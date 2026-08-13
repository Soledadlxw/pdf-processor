#!/usr/bin/env python3
"""笔记文件的读写与 RAG 父子文档切分 —— engine / batch_translate / process_epub 共用。

核心约束：**子文档必须是父笔记正文的逐字切片**。「用子文档命中、用父文档提供
上下文」这套检索方式的前提就是这个，所以这里全程用原文的下标区间来切，任何一块
都是 text[start:end]，不做拼接。
"""

import re
from pathlib import Path

import yaml

DEFAULT_CHUNK_SIZE = 4000

# 都是零宽先行断言：匹配的是分界处那个换行符本身，区间首尾相接、拼起来等于原文
_H2 = r"\n(?=## )"
_H3 = r"\n(?=### )"
_PARA = r"\n(?=\n)"


def read_frontmatter(path: Path) -> dict:
    """读 Markdown 的 YAML frontmatter，读不出来就当空的。"""
    try:
        content = path.read_text()
        if not content.startswith("---"):
            return {}
        return yaml.safe_load(content[3:content.index("---", 3)]) or {}
    except (OSError, ValueError, yaml.YAMLError):
        return {}


def _spans(text: str, pattern: str, start: int, end: int) -> list[tuple[int, int]]:
    bounds = [start]
    for m in re.finditer(pattern, text[start:end]):
        bounds.append(start + m.start())
    bounds.append(end)
    return [(bounds[i], bounds[i + 1]) for i in range(len(bounds) - 1)
            if bounds[i] < bounds[i + 1]]


def _leaf_spans(text: str, start: int, end: int, chunk_size: int) -> list[tuple[int, int]]:
    """把一个区间拆到都不超过 chunk_size。

    降级顺序：### 子小节 → 空行分段 → 硬切。硬切是最后手段，只有既没有子标题
    也没有空行的超长小节才会走到。
    """
    if end - start <= chunk_size:
        return [(start, end)]

    for pattern in (_H3, _PARA):
        parts = _spans(text, pattern, start, end)
        if len(parts) > 1:
            out = []
            for s, e in parts:
                out.extend(_leaf_spans(text, s, e, chunk_size))
            return out

    return [(s, min(s + chunk_size, end)) for s in range(start, end, chunk_size)]


def split_sections(text: str, chunk_size: int = DEFAULT_CHUNK_SIZE) -> list[str]:
    """把正文切成都不超过 chunk_size 的块，每块都是原文的逐字切片。

    先按 ## 小节切，小节超预算的再往下降级，最后把相邻的小块攒满。
    """
    if not text.strip():
        return []

    leaves: list[tuple[int, int]] = []
    for s, e in _spans(text, _H2, 0, len(text)):
        leaves.extend(_leaf_spans(text, s, e, chunk_size))

    chunks: list[str] = []
    cur_start = cur_end = None
    for s, e in leaves:
        if cur_start is None:
            cur_start, cur_end = s, e
        elif e - cur_start <= chunk_size:
            cur_end = e
        else:
            chunks.append(text[cur_start:cur_end])
            cur_start, cur_end = s, e
    if cur_start is not None:
        chunks.append(text[cur_start:cur_end])

    return [c for c in (c.strip() for c in chunks) if c]


def section_titles(chunk: str, limit: int = 6) -> list[str]:
    """取出块内的 ## / ### 标题，写进子文档的 frontmatter。

    子文档单独被检索到时，正文可能只是一句「## 治疗」开头的片段，看不出在讲什么。
    把小节名放进 frontmatter 既能补上这个信息，又不动正文、不破坏逐字切片。
    """
    return re.findall(r"^#{2,3} +(.+?)\s*$", chunk, re.MULTILINE)[:limit]


def write_children(parent_text: str, parent_fm: dict, parent_path: Path,
                   chunk_size: int = DEFAULT_CHUNK_SIZE,
                   stale_names: set[str] | None = None) -> list[Path]:
    """重建 parent_path 的 _chunks/ 子文档，返回写出的文件。

    只切出一块时不写任何文件：那一块会是父笔记正文的逐字复制，在检索里等于把
    同一段内容索引两遍，命中会重复。
    """
    chunks_dir = parent_path.parent / "_chunks"
    stale = {parent_path.name} | (stale_names or set())
    if chunks_dir.exists():
        for cf in chunks_dir.glob("*.md"):
            if read_frontmatter(cf).get("child_of") in stale:
                cf.unlink()

    chunks = split_sections(parent_text, chunk_size)
    if len(chunks) <= 1:
        return []

    chunks_dir.mkdir(exist_ok=True)
    written = []
    for i, body in enumerate(chunks, start=1):
        fm = dict(parent_fm)
        fm["child_of"] = parent_path.name
        fm["parent"] = parent_path.name
        fm["child_index"] = i
        titles = section_titles(body)
        if titles:
            fm["sections"] = titles
        # 文件名带上父笔记的 stem（已含章号），重名章节的子文档不会互相覆盖
        path = chunks_dir / f"{parent_path.stem}-{i}.md"
        yaml_fm = yaml.dump(fm, allow_unicode=True, default_flow_style=False).strip()
        path.write_text(f"---\n{yaml_fm}\n---\n\n{body}")
        written.append(path)
    return written
