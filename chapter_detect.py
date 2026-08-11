#!/usr/bin/env python3
"""章节检测 — 从 PyMuPDF Document 中自动识别章节边界

检测策略（按优先级降级）：
  1. PyMuPDF 内置 TOC（最可靠）
  2. 正则匹配 "Chapter N" / "CHAPTER N" 模式
  3. 字号突变（标题字号 > 正文）
  4. 降级：按页数等分
"""

import re
import fitz


def _is_valid_chapter_list(chapters: list[dict], total_pages: int) -> bool:
    """验证章节划分是否合理"""
    if len(chapters) < 2:
        return False
    if len(chapters) >= 50:
        # 大量章节说明递归下钻成功，直接信任
        return True
    spans = sorted(
        [(c.get("end_page", 0) - c.get("start_page", 0)) for c in chapters],
        reverse=True,
    )
    if len(spans) >= 2 and spans[0] > 0 and spans[1] > 0:
        # 放宽到 8 倍（教材中某些大章可长达200页，小章仅10页）
        if spans[0] > spans[1] * 8:
            return False
    return True


def detect_chapters(doc: fitz.Document, text: str) -> list[dict]:
    """
    返回: [
      {
        "title": "Introduction",
        "pages": "1-12",
        "start_page": 0,
        "end_page": 11,
        "text": "...chapter full text..."
      },
      ...
    ]
    """
    total_pages = len(doc)

    # 策略 1: TOC
    result = _detect_by_toc(doc)
    if result and _is_valid_chapter_list(result, total_pages):
        return result

    # 策略 2: 正则
    result = _detect_by_regex(text)
    if result and _is_valid_chapter_list(result, total_pages):
        return result

    # 策略 3: 字号
    result = _detect_by_font_size(doc)
    if result and _is_valid_chapter_list(result, total_pages):
        return result

    # 策略 4: 降级
    return _fallback_by_pages(doc)


def _detect_by_toc(doc: fitz.Document) -> list[dict] | None:
    """通过 PyMuPDF 内置 TOC 检测，递归下钻到合适粒度（5-200页/章）"""
    toc = doc.get_toc(simple=False)
    if not toc or len(toc) < 2:
        return None

    skip_keywords = [
        "series page", "title page", "copyright", "contents",
        "index", "about the author", "about guilford",
        "discover more", "halftitle", "front cover",
        "instructions for online", "table of contents",
        "dedication", "acknowledgment",
    ]

    def _is_content(title: str) -> bool:
        t = title.lower().strip()
        return not any(k in t for k in skip_keywords)

    max_level = max(e[0] for e in toc)

    def _drill_down(entries_at_level: list, current_level: int, parent_end: int) -> list[dict]:
        """递归下钻：对跨度>80页的条目，用下一级子条目替代"""
        if current_level >= max_level:
            # 已达最深层，转为 dict
            return _toc_entries_to_dicts(entries_at_level, current_level, parent_end)

        result = []
        for i, entry in enumerate(entries_at_level):
            start = entry[2] - 1
            end = (entries_at_level[i + 1][2] - 2
                   if i + 1 < len(entries_at_level)
                   else parent_end)
            span = end - start

            if span > 80 and current_level + 1 <= max_level:
                # 查找下一级子条目
                children = [
                    e for e in toc
                    if e[0] == current_level + 1
                    and _is_content(e[1])
                    and e[2] - 1 >= start
                    and e[2] - 1 <= end + 5
                ]
                if len(children) >= 2:
                    # 递归继续下钻
                    drilled = _drill_down(children, current_level + 1, end)
                    if drilled and len(drilled) >= 2:
                        result.extend(drilled)
                        continue

            # 不下钻，保留当前条目
            result.append({
                "title": entry[1].strip(),
                "start_page": max(0, start),
                "end_page": min(end, parent_end),
                "method": f"toc-l{current_level}",
            })

        return result

    def _toc_entries_to_dicts(entries: list, level: int, parent_end: int) -> list[dict]:
        """将原始 TOC 条目转为标准 dict 格式"""
        result = []
        for i, entry in enumerate(entries):
            start = entry[2] - 1
            end = (entries[i + 1][2] - 2 if i + 1 < len(entries) else parent_end)
            result.append({
                "title": entry[1].strip(),
                "start_page": max(0, start),
                "end_page": min(end, parent_end),
                "method": f"toc-l{level}",
            })
        return result

    # 从 level 1 开始，过滤内容条目
    level1 = [e for e in toc if e[0] == 1 and _is_content(e[1])]
    if len(level1) < 1:
        # 如果没有 level-1 内容条目（如 Nelson 把内容放在 level-2），从 level-2 开始
        level1 = [e for e in toc if e[0] == 2 and _is_content(e[1])]
        if len(level1) >= 2:
            chapters = _drill_down(level1, 2, len(doc) - 1)
        else:
            return None
    else:
        chapters = _drill_down(level1, 1, len(doc) - 1)

    # 后处理: 确保 chapters 是 dict 列表
    if chapters and not isinstance(chapters[0], dict):
        chapters = _toc_entries_to_dicts(chapters, max_level, len(doc) - 1)
    # 1. 过滤太短的条目（<2 页）
    chapters = [c for c in chapters if c.get("end_page", 0) - c.get("start_page", 0) >= 2]
    # 2. 合并过短的相邻条目
    merged = _merge_short_neighbors(chapters)
    # 3. 去重（同一起始页只保留一个）
    seen_pages = set()
    deduped = []
    for c in merged:
        sp = c["start_page"]
        if sp not in seen_pages:
            seen_pages.add(sp)
            deduped.append(c)

    return deduped if len(deduped) >= 4 else None


def _merge_short_neighbors(chapters: list[dict], min_pages: int = 3) -> list[dict]:
    """合并过于短的相邻章节（< min_pages 页）到前一章"""
    if len(chapters) < 3:
        return chapters
    merged = []
    i = 0
    while i < len(chapters):
        ch = dict(chapters[i])
        span = ch["end_page"] - ch["start_page"]
        if span < min_pages and merged:
            # 合并到前一个章节
            merged[-1]["end_page"] = ch["end_page"]
            merged[-1]["title"] += f" + {ch['title']}"
        else:
            merged.append(ch)
        i += 1
    return merged


def _detect_by_regex(text: str) -> list[dict] | None:
    """正则匹配 Chapter/CHAPTER 标记，按章节号去重，保留标题最长者"""
    pattern = re.compile(
        r"Chapter\s+(\d+)[:\s]+(.+?)$",
        re.MULTILINE,
    )
    all_matches = list(pattern.finditer(text))
    if len(all_matches) < 4:  # 少于 4 个匹配不可靠，降级到字号或均分
        return None

    # 按章节号分组，保留标题最长的那条（真章节 > 交叉引用）
    best: dict[int, re.Match] = {}
    for m in all_matches:
        num = int(m.group(1))
        title = m.group(2).strip()
        if num not in best or len(title) > len(best[num].group(2).strip()):
            best[num] = m

    # 交叉引用信号词（以这些词开头的不是章节标题）
    xref_starters = r'^(provides|is|discusses|reviews|on|examines|presents|are|for|was|were|has|have|can|may|will|also|often|typically|in|at|to|as|such|many|most|some|these|those|this|that|the|a|an|and|or|but|with|without|from|of|by|about|into|through|during|before|after|if|when|while|because|although)'

    matches = []
    for num in sorted(best.keys()):
        m = best[num]
        title = m.group(2).strip()
        # 过滤交叉引用
        if re.match(xref_starters, title, re.IGNORECASE):
            continue
        # 真章节：末尾有页码 dots ... N，或标题长度合理
        has_page = re.search(r"\.\s*\.\s*\.\s*\.\s*\d+\s*$", title)
        if has_page or len(title) >= 15:
            matches.append(m)

    if len(matches) < 4:
        return None

    # 按文本位置排序（非章节号），确保 text[pos:next_pos] 边界正确
    matches = sorted(matches, key=lambda m: m.start())

    chapters = []
    for i, m in enumerate(matches):
        num = int(m.group(1))
        title = f"Chapter {num}: {m.group(2).strip()}"
        pos = m.start()
        next_pos = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        chapter_text = text[pos:next_pos].strip()

        chapters.append({
            "title": title,
            "start_page": -1,
            "end_page": -1,
            "text_override": chapter_text,
            "method": "regex",
        })

    return chapters


def _detect_by_font_size(doc: fitz.Document) -> list[dict] | None:
    """通过字号突变检测章节标题"""
    # 收集每页的最大字号
    page_max_fonts = {}
    for page_num in range(min(len(doc), 50)):  # 采样前 50 页
        page = doc[page_num]
        blocks = page.get_text("dict")["blocks"]
        max_size = 0
        for b in blocks:
            if "lines" not in b:
                continue
            for line in b["lines"]:
                for span in line["spans"]:
                    max_size = max(max_size, span["size"])
        page_max_fonts[page_num] = max_size

    if not page_max_fonts:
        return None

    # 全局字号中位数（正文字号）
    sizes = list(page_max_fonts.values())
    body_size = sorted(sizes)[len(sizes) // 3]  # 取下三分位数

    # 字号显著大于正文的页面可能是章节首页
    title_threshold = body_size * 1.3
    candidate_pages = [
        p for p, s in page_max_fonts.items()
        if s > title_threshold and s > 10
    ]

    if len(candidate_pages) < 2:
        return None

    # 去重相邻页（同一章节标题跨页）
    deduped = [candidate_pages[0]]
    for p in candidate_pages[1:]:
        if p - deduped[-1] > 2:  # 至少间隔 2 页
            deduped.append(p)

    # 提取每章标题（该页最大字体块）
    chapters = []
    for i, page_num in enumerate(deduped):
        title = _extract_title_from_page(doc[page_num])
        if not title:
            title = f"Section {i + 1}"

        end_page = (
            deduped[i + 1] - 1
            if i + 1 < len(deduped)
            else len(doc) - 1
        )

        chapters.append({
            "title": title,
            "start_page": page_num,
            "end_page": end_page,
            "method": "font-size",
        })

    return chapters if len(chapters) >= 2 else None


def _extract_title_from_page(page: fitz.Page) -> str:
    """从页面提取最大字体文本作为标题候选"""
    blocks = page.get_text("dict")["blocks"]
    candidates = []
    for b in blocks:
        if "lines" not in b:
            continue
        for line in b["lines"]:
            for span in line["spans"]:
                candidates.append((span["size"], span["text"].strip()))
    if not candidates:
        return ""
    candidates.sort(key=lambda x: -x[0])
    # 取最大字体的前两行
    top_texts = [t for _, t in candidates[:2] if len(t) > 3]
    return " — ".join(top_texts[:2])


def _fallback_by_pages(doc: fitz.Document) -> list[dict]:
    """降级方案：每 30 页一块"""
    chapters = []
    chunk_size = 30
    for start in range(0, len(doc), chunk_size):
        end = min(start + chunk_size - 1, len(doc) - 1)
        chapters.append({
            "title": f"pp. {start + 1}–{end + 1}",
            "start_page": start,
            "end_page": end,
            "method": "fallback",
        })
    return chapters


def extract_chapter_text(doc: fitz.Document, chapter: dict,
                         cite_key: str = "",
                         header_ratio: float = 0.08,
                         footer_ratio: float = 0.08) -> str:
    """提取章节对应的页面文本（简易版，不调 cleaner）"""
    seen_lines: dict[str, int] = {}
    page_texts = []

    start = chapter.get("start_page", 0)
    end = chapter.get("end_page", len(doc) - 1)

    for page_num in range(max(0, start), min(end + 1, len(doc))):
        page = doc[page_num]
        height = page.rect.height

        blocks = page.get_text("blocks")
        blocks.sort(key=lambda b: (round(b[1] / 20) * 20, b[0]))

        for b in blocks:
            if b[6] != 0:
                continue
            y = b[1]
            if y < height * header_ratio or y > height * (1 - footer_ratio):
                continue
            text = b[4].strip()
            if text.isdigit() or not text:
                continue
            normalized = text.lower().strip()
            seen_lines[normalized] = seen_lines.get(normalized, 0) + 1
            if seen_lines[normalized] > 3:
                continue
            page_texts.append(text)

    full_text = "\n\n".join(page_texts)

    # 截断参考文献
    from cleaner import trim_references
    full_text = trim_references(full_text)

    return full_text
