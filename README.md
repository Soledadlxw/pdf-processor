# PDF/EPUB 书籍处理流水线

通过本地 Ollama 大模型将医学 PDF 和历史 EPUB 处理为中文结构化 Obsidian 笔记。

## 依赖

- Python 3.12+
- [Ollama](https://ollama.com) 本地运行
- 模型：`gemma4:31b-mlx`（~28GB）、`qwen3.6:35b-mlx`（~30GB）

```bash
ollama pull gemma4:31b-mlx
ollama pull qwen3.6:35b-mlx
pip install pymupdf ollama pyyaml

# 仅在 configs/*.yaml 里开了 table_extraction 时需要
pip install 'camelot-py[base]'
```

## 脚本一览

| 脚本 | 用途 |
|------|------|
| `engine.py` | 医学 PDF 统一引擎 |
| `batch_translate.py` | Phase 2 批量中译 + 标题中文化 |
| `process_epub.py` | EPUB 历史书处理 |
| `translate_tables.py` | 纯表格书籍逐页翻译（仅 qwen） |
| `chapter_detect.py` | 章节检测（TOC 递归下钻，被 engine 调用） |
| `cleaner.py` | PDF 清洗（被 engine 调用） |
| `cross_check.py` | 跨书矛盾检测 |
| `postprocess/medical.py` | 医学术语词典 |
| `postprocess/history.py` | 历史人物/事件/帝系图谱 |

## 目录结构

```
pdf-processor/
├── books_medical/          # 放医学 PDF（gitignore 排除）
├── books_history/          # 放历史 EPUB（gitignore 排除）
├── configs/                # 领域配置
│   ├── medical.yaml
│   └── history.yaml
├── prompts/                # LLM prompt 模板
│   ├── medical_stage1.txt  # Stage 1 英文提取
│   ├── medical_stage2.txt  # Stage 2 中文翻译
│   └── history_stage1.txt  # 历史中文处理
├── postprocess/            # 后处理（术语词典、关系图谱）
├── metadata.yaml           # 医学书籍元数据
├── metadata_history.yaml   # 历史书籍元数据
└── pdfs/                   # 测试用 PDF
```

## 使用流程

### 1. 医学 PDF（两阶段处理）

#### 1.1 准备

```bash
# 1. 将 PDF 放入 books_medical/
# 2. 编辑 metadata.yaml 添加书籍元数据
# 3. 章节检测验证
python3 engine.py --config configs/medical.yaml --book "书名关键词" --dry-run
```

#### 1.2 Phase 1 — 英文提取（仅 gemma4）

```bash
python3 engine.py --config configs/medical.yaml --book "书名" --fast --resume
```

- `--fast`：只跑 Stage 1，不加载 qwen
- `--resume`：跳过已完成章节（输出 > 2KB）
- `Control+C` 停止，下次同一命令续跑
- 可选 `--chapters "1,3,5-10"` 只处理指定章节

#### 1.3 Phase 2 — 中文翻译（仅 qwen）

```bash
python3 batch_translate.py "~/note/Book Notes/书名/"
```

- 自动跳过已翻译章节
- 自动翻译章节标题、提炼 5-8 字短标签
- 自动重命名文件为中文
- 自动重生成面包屑导航
- 大章（>15K 字符）自动分块翻译
- `_chunks/` 子文档由翻译后的中文正文直接切分生成，不再逐个送 LLM

#### 1.4 后处理

```bash
python3 -m postprocess.medical    # 术语词典
python3 cross_check.py            # 跨书矛盾检测
```

### 2. 历史 EPUB

```bash
# 1. EPUB 放入 books_history/
# 2. 编辑 metadata_history.yaml
# 3. 验证章节
python3 process_epub.py --dry-run

# 4. 正式处理（qwen 单阶段）
python3 process_epub.py

# 5. 生成图谱
python3 -m postprocess.history
```

### 3. 纯表格书籍翻译（如评估量表手册附录）

```bash
python3 translate_tables.py "books_medical/Vineland 3 Manual.pdf"
```

- 只调 qwen，不加载 gemma4，内存占用低
- 逐页翻译英文文本，保留所有数字、分数、统计值和表格结构
- 适用于常模表、年龄当量表等纯数据附录
- 按字符预算分批（≤12000 字符或 ≤10 页），不会因为固定页数把后几页挤出 prompt
- 模型漏页时自动单页重翻；仍失败则写入英文原文并标 `status: untranslated`
- 结束时校验「非空页数 = 落盘文件数」，有缺页会报错退出

### 4. engine.py 全部选项

```
--book "关键词"       只处理匹配名称的书
--chapters "1,3,5-10" 只处理指定章节
--fast               仅 Stage 1（英文提取）
--bilingual          中英对照输出
--retry-failed       重试空壳章节（< 2KB）
--resume             跳过已完成章节
--dry-run            只看章节检测，不调 LLM
--init               生成 metadata 模板
```

## metadata.yaml 示例

```yaml
新书.pdf:
  cite_key: 'KEY'
  title: 'Book Full Title'
  title_zh: '中文书名'
  authors:
  - 'Author Name'
  edition: 1
  year: 2020
  publisher: 'Publisher'
  isbn: '978-...'
  dsm_version: 'DSM-5'
  evidence_level: 'gold-standard'
  book_type: 'textbook'
  tags: [tag1, tag2]
```

字段说明：
- `cite_key`：必需，3-6 字符大写缩写，用于引用标注
- `title_zh`：必需，输出目录名和 Obsidian 显示
- `book_type`：必需，如 `textbook` / `clinical-manual` / `parent-guide`
- `dsm_version`：用于时效性检查，非 DSM 相关可省略
- `evidence_level`：证据等级标注

## 输出

每个章节生成一个 Markdown 文件：

```
~/note/Book Notes/中文书名/
├── _中文书名.md          # 全书索引
├── 01-第一章中文标题.md   # 章节笔记（含面包屑导航）
├── 02-第二章中文标题.md
└── _chunks/              # RAG 父子文档
    ├── 01-第一章-1.md
    └── 02-第二章-1.md
```

笔记包含：
- YAML frontmatter（元数据、时效性标注）
- 结构化正文（诊断标准、评估工具、干预方案、关键事件等）
- `[[...]]` 面包屑导航（← 上一章 | 下一章 →）
- 引用标注 `[CITE_KEY:ChX:p.YY]`

## 注意事项

1. **两个模型不能同时驻留**（64GB Mac 内存不足），分 Phase 跑或让引擎逐章切换
2. **大 PDF 首次加载需 3-5 分钟**（如 191MB 的 Nelson）
3. **Ollama 偶发断连**，引擎内置 3 次重试，间隔 30 秒
4. **章节检测依赖 PDF 内嵌 TOC**，扫描版 PDF 会降级到页数等分
5. **Obsidian Mermaid 兼容**：已自动处理 `<br/>`、`|`、`%%` 注释等不兼容语法
6. **`_chunks/`** 目录为 RAG 子文档，是父笔记正文的逐字切片（子文档命中、父文档供上下文），Phase 2 翻译后按中文正文重新生成
7. **batch_translate.py 大章处理**：>15K 字符自动按 `##` 切分，单块超时 600s

## 已修复的坑

- TOC 递归下钻：支持 4 层嵌套（如 Nelson 的 Volume → Part → Chapter）
- 断点续跑：`--resume` 基于文件大小判断，不会漏章
- Mermaid 兼容：去掉 `<br/>`、标签内 `|`、中文 `%%` 注释
- `_index.md` 重名：已改为 `_{书名}.md`，Obsidian 图谱不冲突
- 面包屑导航：Phase 2 翻译后自动重生成中文导航链接
