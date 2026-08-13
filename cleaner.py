#!/usr/bin/env python3
"""PDF 清洗模块 — 双栏重排、页眉页脚去除、表格提取、参考文献截断"""

import os
import fitz  # PyMuPDF


def markdown_table(rows: list[list[str]]) -> str:
    """将二维列表转为 Markdown 表格"""
    if not rows or not rows[0]:
        return ""
    # 第一行为表头
    header = rows[0]
    n_cols = len(header)
    lines = []
    lines.append("| " + " | ".join(str(c) for c in header) + " |")
    lines.append("|" + "|".join([":---:"] * n_cols) + "|")
    for row in rows[1:]:
        # 补齐可能缺少的列
        padded = list(row) + [""] * (n_cols - len(row))
        lines.append("| " + " | ".join(str(c) for c in padded[:n_cols]) + " |")
    return "\n".join(lines)


def extract_tables_from_page(page: fitz.Page, cite_key: str,
                              chapter_num: int) -> list[dict]:
    """从页面提取所有表格，返回 {markdown, page, bbox} 列表"""
    results = []
    try:
        tables = page.find_tables()
    except Exception:
        return results

    for t in tables:
        try:
            rows = t.extract()
        except Exception:
            continue
        if not rows or len(rows) < 2:
            continue  # 单行不是有效表格

        md = markdown_table(rows)
        citation = f"\n\n> [{cite_key}:Ch{chapter_num}:Table, p.{page.number + 1}]\n"
        results.append({
            "markdown": md + citation,
            "page": page.number,
            "bbox": t.bbox,  # (x0, y0, x1, y1) — 用于定位插入位置
        })
    return results


def clean_page_blocks(page: fitz.Page,
                      header_ratio: float = 0.08,
                      footer_ratio: float = 0.08) -> list[str]:
    """按阅读顺序提取一页的正文块，去掉页眉页脚带内的内容和纯页码。"""
    height = page.rect.height
    blocks = page.get_text("blocks")
    # 按阅读顺序排序：先垂直、再水平（处理双栏）
    blocks.sort(key=lambda b: (round(b[1] / 20) * 20, b[0]))

    clean_blocks = []
    for b in blocks:
        # type 0 = 文本, type 1 = 图片（跳过）
        if b[6] != 0:
            continue

        y = b[1]
        # 页眉（顶部 header_ratio）／页脚（底部 footer_ratio）
        if y < height * header_ratio or y > height * (1 - footer_ratio):
            continue

        text = b[4].strip()
        # 空行、纯页码
        if not text or text.isdigit():
            continue

        clean_blocks.append(text)

    return clean_blocks


def drop_running_headers(pages_lines: list[list[str]],
                         min_pages: int = 4,
                         min_fraction: float = 0.5,
                         max_len: int = 100) -> list[list[str]]:
    """去掉漏出页眉页脚带、在大部分页面重复出现的短行。

    只有「短」且「至少出现在一半页面上」的行才算跑版页眉。教材里 Treatment、
    Etiology 这类小标题会在少数页面重复出现，不能跟着一起删。
    """
    n_pages = len(pages_lines)
    if n_pages < min_pages:
        return pages_lines

    threshold = max(min_pages, int(n_pages * min_fraction))
    counts: dict[str, int] = {}
    for lines in pages_lines:
        # 每页同一行只计一次，避免单页内的重复抬高计数
        for norm in {l.strip().lower() for l in lines if len(l) <= max_len}:
            counts[norm] = counts.get(norm, 0) + 1

    repeated = {t for t, c in counts.items() if c >= threshold}
    if not repeated:
        return pages_lines

    return [
        [l for l in lines if len(l) > max_len or l.strip().lower() not in repeated]
        for lines in pages_lines
    ]


def clean_book_pages(doc: fitz.Document, cite_key: str = "",
                     chapter_num: int = 0,
                     header_ratio: float = 0.08,
                     footer_ratio: float = 0.08,
                     extract_tables: bool = False) -> tuple[str, list[dict]]:
    """
    从 PyMuPDF Document 提取并清洗正文。

    extract_tables 默认关闭：find_tables() 比取文字慢约 60 倍，只有真正要用表格的
    调用方才应该打开它。

    返回:
        text: 清洗后的正文（extract_tables 打开时在末尾附上表格 Markdown）
        all_tables: 抽出的表格列表 [{markdown, page, bbox}, ...]
    """
    all_tables: list[dict] = []
    pages_lines: list[list[str]] = []

    for page_num in range(len(doc)):
        page = doc[page_num]

        if extract_tables:
            all_tables.extend(extract_tables_from_page(page, cite_key, chapter_num))

        pages_lines.append(clean_page_blocks(page, header_ratio, footer_ratio))

    pages_lines = drop_running_headers(pages_lines)
    text = "\n\n".join("\n".join(lines) for lines in pages_lines if lines)

    # ── 参考文献截断 ──
    text = trim_references(text)

    # ── 表格插入 ──
    # 在正文末尾附加所有表格（理想情况应按位置插入，简化处理）
    if all_tables:
        text += "\n\n## 本章表格\n\n"
        for t in all_tables:
            text += t["markdown"] + "\n\n"

    return text, all_tables


def trim_references(text: str) -> str:
    """从后往前找到 References/Bibliography 并截断"""
    markers = [
        "\nReferences\n", "\nREFERENCES\n",
        "\nBibliography\n", "\nBIBLIOGRAPHY\n",
        "\nReferences  \n", "\nBibliography  \n",
    ]
    best_cut = len(text)
    for marker in markers:
        idx = text.rfind(marker)
        if idx > len(text) * 0.6:  # 必须在文本后半部分
            best_cut = min(best_cut, idx)

    if best_cut < len(text) * 0.9:
        return text[:best_cut]
    return text


def extract_metadata(doc: fitz.Document) -> dict:
    """提取 PDF 内嵌元数据"""
    meta = doc.metadata
    return {
        "title": meta.get("title", ""),
        "author": meta.get("author", ""),
        "subject": meta.get("subject", ""),
        "keywords": meta.get("keywords", ""),
        "year": "",  # PDF 元数据通常不含出版年份，需手动填写
    }


def classify_page(page: fitz.Page) -> str:
    """判断页面是文字页还是图表/表格页

    检测策略（按优先级）：
      1. 嵌入式图片面积 > 30% → image
      2. 文字密度极低（< 100 chars） → image（空白页/纯图页）
      3. 文字密度偏低（< 800 chars）+ 无连续长段落 → image（表格/表单页）
      4. 其他 → text

    返回: "text" | "image"
    """
    blocks = page.get_text("blocks")

    text_chars = sum(len(b[4]) for b in blocks if b[6] == 0)
    image_blocks = [b for b in blocks if b[6] == 1]
    image_area = sum(
        (b[2] - b[0]) * (b[3] - b[1]) for b in image_blocks
    )
    page_area = page.rect.width * page.rect.height

    # 1. 嵌入图片面积 > 30%
    if page_area > 0 and image_area / page_area > 0.3:
        return "image"

    # 2. 文字极少 → 空白页或纯图页
    if text_chars < 200:
        return "image"

    # 3. 文字密度偏低 + 没有连续文本段落 → 表格/表单页（非正文页）
    if text_chars < 1500:
        long_paragraphs = 0
        for b in blocks:
            if b[6] == 0 and len(b[4]) > 300:
                long_paragraphs += 1
        if long_paragraphs == 0:
            return "image"

    return "text"


def render_page_as_png(page: fitz.Page, output_path: str, dpi: int = 200):
    """将页面渲染为 PNG 图片"""
    pix = page.get_pixmap(dpi=dpi)
    pix.save(output_path)


def describe_image(png_path: str, chapter_title: str = "",
                   model: str = "gemma4:31b-mlx") -> str:
    """用视觉 LLM 生成图片的一句话中文描述"""
    try:
        from ollama import Client
        client = Client(host="http://localhost:11434", timeout=120)
        prompt = "用一句中文描述这张图/表格的内容。如果是表格，说明有哪些列和大致内容。"
        if chapter_title:
            prompt = f"这张图来自章节「{chapter_title}」。{prompt}"
        response = client.chat(
            model=model,
            messages=[{"role": "user", "content": prompt,
                       "images": [png_path]}],
            options={"temperature": 0},
        )
        return response["message"]["content"].strip()
    except Exception as e:
        return f"[图表: 见原书对应位置]"


def extract_table_camelot(pdf_path: str, page_num: int) -> list[list[str]] | None:
    """用 Camelot 提取表格（优先 lattice，降级 stream）

    page_num: 1-indexed 页码
    返回: 二维列表（首行为表头），失败返回 None
    """
    try:
        import camelot
        # 先试 lattice（有边框表格）
        tables = camelot.read_pdf(
            pdf_path, pages=str(page_num), flavor="lattice",
            suppress_stdout=True,
        )
        if tables.n == 0:
            # 降级 stream（无边框表格）
            tables = camelot.read_pdf(
                pdf_path, pages=str(page_num), flavor="stream",
                suppress_stdout=True,
            )
        if tables.n > 0:
            # 取第一个表格，转二维列表
            df = tables[0].df
            rows = df.values.tolist()
            # 过滤全空行
            rows = [r for r in rows if any(str(c).strip() for c in r)]
            return rows if len(rows) >= 2 else None
    except Exception:
        pass
    return None


def extract_table_vision(png_path: str,
                         model: str = "gemma4:31b-mlx") -> list[list[str]] | None:
    """用 Vision LLM 提取完整表格结构

    返回: 二维列表（首行为表头），失败返回 None
    """
    try:
        from ollama import Client
        client = Client(host="http://localhost:11434", timeout=180)
        prompt = """Extract this table as a CSV-like format.
Rules:
- First row is the header
- Preserve ALL rows and columns exactly as shown
- If a cell is empty, leave it blank
- Output ONLY the data rows, comma-separated, one row per line
- No markdown formatting, no explanations

Example output:
Name,Age,Score
Alice,5,87
Bob,6,92

Table data:"""
        response = client.chat(
            model=model,
            messages=[{"role": "user", "content": prompt,
                       "images": [png_path]}],
            options={"temperature": 0},
        )
        content = response["message"]["content"].strip()
        # 解析 CSV
        rows = []
        for line in content.split("\n"):
            line = line.strip()
            if line and "," in line:
                rows.append([c.strip() for c in line.split(",")])
        return rows if len(rows) >= 2 else None
    except Exception:
        return None


def extract_table_markdown(pdf_path: str, page_num: int) -> str | None:
    """提取表格并返回 Markdown 格式（仅 Camelot，Vision 不可靠）"""
    rows = extract_table_camelot(pdf_path, page_num)
    if not rows:
        return None

    # 转为 Markdown table
    lines = []
    lines.append("| " + " | ".join(str(c) for c in rows[0]) + " |")
    lines.append("|" + "|".join([":---:"] * len(rows[0])) + "|")
    for row in rows[1:]:
        padded = list(row) + [""] * (len(rows[0]) - len(row))
        lines.append("| " + " | ".join(str(c) for c in padded[:len(rows[0])]) + " |")

    return "\n".join(lines) + f"\n\n> [表格: Camelot 提取, p.{page_num}]\n"


def split_chapter_pages(doc: fitz.Document, start_page: int,
                        end_page: int,
                        header_ratio: float = 0.08,
                        footer_ratio: float = 0.08,
                        trim_refs: bool = True) -> tuple[str, list[dict]]:
    """
    将章节页面分流为文字和图表，文字部分做与 clean_book_pages 相同的清洗。

    这里的返回值就是最终送进 LLM 的正文，所以页眉页脚裁剪、纯页码剔除、跑版页眉
    去重和参考文献截断都必须在这里生效。

    返回:
        text: 清洗后的章节正文，段落之间以空行分隔（供按段落分块）
        image_parts: [{page, png_basename}, ...]
    """
    pages_lines: list[list[str]] = []
    image_parts = []

    for pg in range(start_page, end_page + 1):
        page = doc[pg]

        if classify_page(page) == "text":
            pages_lines.append(clean_page_blocks(page, header_ratio, footer_ratio))
        else:
            image_parts.append({
                "page": pg + 1,
                "png_basename": f"p{pg+1}.png",
            })

    pages_lines = drop_running_headers(pages_lines)

    # 用空行分隔段落：engine 的 chunk_text_by_paragraphs 按 "\n\n" 切块，
    # 用 "\n" 拼会让整章变成一个段落，max_chars_per_chapter 就形同虚设。
    text = "\n\n".join(line for lines in pages_lines for line in lines)

    if trim_refs:
        text = trim_references(text)

    return text, image_parts
