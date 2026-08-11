# PDF/EPUB 书籍处理流水线

## 项目概述

将医学 PDF 和历史 EPUB 通过本地 Ollama 大模型处理为中文结构化笔记（Obsidian Markdown），支持 RAG 父子文档、术语词典、人物关系图谱。

## 模型

| 模型 | 用途 | 内存 |
|------|------|------|
| `gemma4:31b-mlx` | Stage 1 英文提取 | ~28GB |
| `qwen3.6:35b-mlx` | Stage 2 中文翻译 / 中文处理 | ~30GB |

**注意**：两个模型不能同时驻留内存（64GB Mac 扛不住），必须分阶段跑或逐章切换。

## 核心脚本

| 脚本 | 用途 |
|------|------|
| `engine.py` | **统一引擎**（医学 PDF 主力） |
| `batch_translate.py` | Phase 2 批量翻译 + 标题中文化 |
| `process_epub.py` | EPUB 历史书处理 |
| `chapter_detect.py` | 章节检测（TOC 递归下钻） |
| `cleaner.py` | PDF 清洗（被 engine 调用） |
| `cross_check.py` | 跨书矛盾检测 |
| `postprocess/medical.py` | 医学术语词典 |
| `postprocess/history.py` | 历史人物/事件/帝系图谱 |

---

## 工作流 1：医学 PDF（主力流程）

### 第一步：准备

```bash
cd ~/project/pdf-processor

# 1. 确保 PDF 在 books/ 下
# 2. 在 metadata.yaml 添加书籍元数据
# 3. Dry-run 验证章节检测
python3 engine.py --config configs/medical.yaml --book "书名关键词" --dry-run
```

### 第二步：Phase 1 — 英文提取（仅 gemma4，推荐分阶段跑）

```bash
# --fast 只跑 Stage 1，模型不切换
# --resume 跳过已完成章节（输出 > 2KB）
python3 engine.py --config configs/medical.yaml --book "Nelson" --fast --resume

# Control-C 停止，下次同一命令续跑
```

### 第三步：Phase 2 — 中文翻译（仅 qwen）

```bash
# Phase 1 全部完成后执行
python3 batch_translate.py "~/note/Book Notes/书名/"
```

### 第四步：后处理

```bash
# 术语词典
python3 -m postprocess.medical
# 跨书矛盾检测
python3 cross_check.py
```

### 其他 engine.py 选项

```bash
--book "关键词"     # 只处理匹配的书
--chapters "1,3,5-10"  # 只处理指定章节
--fast             # 仅 Stage 1（英文）
--bilingual        # 中英对照
--retry-failed     # 重试空壳章节（<2KB）
--resume           # 跳过已完成章节
--dry-run          # 只看章节检测，不调 LLM
--init             # 生成 metadata 模板
```

---

## 工作流 2：历史 EPUB

### 准备

1. EPUB 放 `books_history/`
2. 在 `metadata_history.yaml` 添加元数据
3. 如有需要，创建 `configs/history.yaml` 和 `prompts/history_stage1.txt`

### 处理

```bash
python3 process_epub.py --dry-run          # 验章节
python3 process_epub.py                    # 正式处理（qwen 单阶段）
python3 -m postprocess.history             # 生成人物/事件/帝系图
```

---

## 工作流 3：增量处理新书（推荐分阶段）

```
新书 PDF → metadata.yaml → dry-run 验章节 → 确认无误
  → engine.py --fast --resume（Phase 1，gemma4，每天跑几小时）
  → 全部 Phase 1 完成
  → batch_translate.py（Phase 2，qwen，一次性）
  → postprocess/medical.py（术语词典）
```

---

## 已知问题与经验

1. **章节检测**：大教材（Nelson）TOC 有 4 层，`chapter_detect.py` 已修复递归下钻，从 Part 级钻到 Chapter 级
2. **模型切换**：`engine.py` 在 full 模式下每章 Stage1→卸载→Stage2→卸载，避免两模型同时占内存
3. **分阶段更高效**：`--fast` + `batch_translate.py` 分开跑，每个阶段只有一个模型
4. **大章处理**：`batch_translate.py` 自动按 `##` 切分 > 15K 字符的章节
5. **Mermaid 兼容**：Obsidian 的 Mermaid 不支持 `<br/>`、`|` 在标签中、`%%` 中文注释
6. **191MB PDF**：大文件首次加载需 3-5 分钟
7. **Ollama 偶发断连**：重试 3 次，间隔 30 秒，仍有概率失败

---

## 已处理书籍

### 医学（8 本）
| 书名 | 章数 | 状态 |
|------|------|------|
| DSM-5-TR 儿童青少年口袋指南 | 20 | ✅ 完成 |
| ESDM 早期丹佛模式 | 12 | ✅ 完成 |
| 与自闭症孩子的早期介入 | 16 | ✅ 完成 |
| 发育行为儿科学 AAP | 28 | ✅ 完成 |
| 我想飞进天空 | 6 | ✅ 完成 |
| 自闭症手册 卷一 | 25 | ✅ 完成 |
| NDBI 自然情境干预 | 14 | ✅ 完成 |
| **Nelson 儿科学教材** | **646** | ✅ 完成 |

### 历史（1 本）
| 书名 | 章数 | 状态 |
|------|------|------|
| 明朝那些事儿 | 159 | ✅ 完成（含人物/事件/帝系图） |
