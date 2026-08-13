# 优化分析报告

对 `engine.py` / `batch_translate.py` / `process_epub.py` / `cleaner.py` / `chapter_detect.py` /
`cross_check.py` / `postprocess/*` 全量代码走查的结果。按「先修正确性、再省算力、最后谈架构」排序。

报告中标注 **实测** 的数字来自本仓库 `pdfs/` 下的 12 页 PDF 实际运行；标注 **推算** 的是按每页耗时
线性外推到大教材规模，仅用于说明量级。

---

## 一、结论速览

最值得先动的三件事：

1. **每次启动都对全书每一页跑一次表格识别，结果直接丢弃。** 实测 180 ms/页，推算 Nelson 规模
   约 10 分钟纯空转，且 `--dry-run` 和 `--resume` 也照付这笔钱。
2. **`cleaner.py` 的清洗逻辑没有作用在真正送给 LLM 的文本上。** 页眉页脚裁剪、页码剔除、重复行
   去重、参考文献截断全部作用在一份被丢弃的副本上；实际入模文本比清洗后的版本多 17%（实测），
   含 75 处纯页码行。
3. **Phase 2 把同一份内容翻译了两遍。** 父笔记翻一次，`_chunks/` 里的每个子文档再各翻一次，
   token 消耗约翻倍，并且破坏了父子文档的文本对齐（RAG 检索到的子文档措辞与父笔记不一致）。

此外有 7 处会静默出错的正确性问题（详见第二节），其中 `postprocess/history.py` 目前已经完全跑不起来。

---

## 二、正确性问题（静默失败，优先修）

### 2.1 `postprocess/history.py` 找不到任何书，直接退出

`main()` 通过 `_index.md` 是否存在来判断一个目录是不是历史书：

`postprocess/history.py` L354–357:

```python
        index_file = d / "_index.md"
        if index_file.exists():
            content = index_file.read_text()
            if "history" in content.lower() or "明朝" in content:
```

但索引文件早已改名为 `_{书名}.md`（README「已修复的坑」里记录了这次改名），`engine.py` 和
`process_epub.py` 都不再写 `_index.md`。结果是 `history_dirs` 恒为空，脚本输出
"No history book directories found." 后 `sys.exit(1)`。

同一次改名还留下三处遗漏：`collect_notes()`、`cross_check.py`、`postprocess/medical.py`
都只跳过 `_index.md`，于是会把 `_{书名}.md`、`_graphs.md` 当成章节笔记喂给 LLM。

**修法**：判定改为 `book_type: history` 出现在任意 `_*.md` 的 frontmatter，或直接读 metadata；
排除条件统一为 `name.startswith("_")`。

### 2.2 `--chapters "5-10"` 会静默地跑全书

`engine.py` L680:

```python
            chapter_filter = set(int(x.strip()) for x in sys.argv[i+1].split(",") if x.strip().isdigit())
```

`isdigit()` 过滤掉了一切区间写法，而下游判断是 `if chapter_filter and ...`——空集合等于「不过滤」：

| 输入 | 解析结果 | 实际行为 |
|------|----------|----------|
| `1,3` | `{1, 3}` | 正确 |
| `5-10` | `set()` | **跑全部 646 章** |
| `1,3,5-10` | `{1, 3}` | **静默丢掉 5–10** |

README 和 CLAUDE.md 都把 `--chapters "1,3,5-10"` 写成了受支持的用法。**修法**：解析区间，并且
在解析结果为空但用户确实传了 `--chapters` 时直接报错退出。

### 2.3 prompt 文件路径写错时，会用空 prompt 跑完整本书

`engine.py` L53–58:

```python
def load_prompt(path: str) -> str:
    if not os.path.exists(path):
        # 相对于 config 文件的目录
        return ""
```

返回 `""` 后，`"".format(...)` 依然是 `""`，于是 `ollama.chat` 收到空 user message，模型返回
无关内容，笔记照写、`--resume` 还会把它当成已完成（只看文件大小 > 2KB）。646 章可以就这么全跑废。
`configs/history.yaml` 没有 `stage2_prompt`，用它跑非 `--fast` 模式就会命中这条路径。

**修法**：prompt 缺失或模板缺少必需占位符时 fail fast。

### 2.4 Phase 2 重命名之后，`--resume` / `--retry-failed` 全部失效

`engine.py` 用英文标题算文件名：

`engine.py` L263–265:

```python
        ch_slug = slugify(ch['title'])
        note_filename = f"{ch_num:02d}-{ch_slug}.md"
        note_path = os.path.join(output_dir, note_filename)
```

而 `batch_translate.py` 会把文件改名成中文（`01-Overview-of-Development.md` → `01-发育概述.md`）。
翻译过一轮之后再跑 `engine.py --resume`，它找不到英文名文件，于是重新处理该章并**额外写出一份
英文笔记**，同一章在目录里出现两次，`write_book_index` 也会把两份都列进索引。

**修法**：以章号为准判断完成状态（`glob(f"{ch_num:02d}-*.md")`），或引入进度清单（见 4.4）。

### 2.5 无 TOC 的 PDF：正则识别出的章节边界被丢弃

`_detect_by_regex` 把真实章节文本放在 `text_override` 里，`start_page/end_page` 设为 `-1`：

`chapter_detect.py` L238–244:

```python
        chapters.append({
            "title": title,
            "start_page": -1,
            "end_page": -1,
            "text_override": chapter_text,
            "method": "regex",
        })
```

`text_override` 全仓库没有任何消费者。`engine.py` 见到 `start_page < 0` 就退化成按总页数均分：

`engine.py` L279–282:

```python
        if ch.get("start_page", -1) < 0:
            per_ch = len(doc) // len(chapters)
            start_pg = i * per_ch
```

于是明明已经识别出正确边界，实际切分却是把前言、正文、索引一起等分。**修法**：`engine.py` 优先使用
`text_override`；用不上就把这个字段删掉，别留误导。

### 2.6 被判为「图表页」的内容在默认配置下彻底丢失

`split_chapter_pages` 用 `classify_page` 分流，判为 `image` 的页只记录页码、不保留文本。而
`configs/medical.yaml` 里 `render_charts: false`、`table_extraction: false`，`engine.py` 中渲染
分支被 `if config.get("render_charts")` 挡住——这些页既不出 PNG 也不出文字，直接消失。

`classify_page` 的阈值对密排的教材页相当激进（文字 < 200 字符，或 < 1500 字符且没有 300+ 字符的
连续段落即判为 image），表格页、清单页、量表页最容易命中。顺带一提，它的 docstring 写的是
「< 100 chars」「< 800 chars」，与代码里的 200/1500 不一致。

**修法**：即使判为图表页也保留其文字作为 fallback；或在 `render_charts: false` 时不做分流。

### 2.7 `_chunks/` 子文档文件名不含章号，同名章节互相覆盖

`engine.py` L109:

```python
        name = f"{slugify(parent_frontmatter.get('book',''))}-{slug}-{i+1}.md"
```

`slug` 来自章节标题。Nelson 这种规模里重名标题（Introduction / Overview / Clinical Manifestations）
非常常见，父笔记有 `NN-` 前缀所以安全，子文档没有——后处理的章会静默覆盖前面章的 chunk。

### 2.8 Phase 2 生成的面包屑「下一章」链接必然是死链

`batch_translate.py` 在翻译第 k 章时立刻算导航：

`batch_translate.py` L208–216:

```python
    all_notes = sorted(old_path.parent.glob("*.md"))
    all_notes = [n for n in all_notes if n.name != "_index.md"]
```

此刻第 k+1 章还是英文名，于是写下 `[[01-Some-English-Name|label]]`；等第 k+1 章被翻译并改名成
中文，这个 wikilink 就断了。**每一章的「下一章」链接都会断**，只有最后一章的「上一章」是对的。
另外这里没有排除 `_{书名}.md`（`_` 排在数字之后），最后一章的「下一章」会指向全书索引。

**修法**：把导航生成拆成所有翻译与重命名完成之后的独立收尾 pass。

---

## 三、算力浪费（可直接换成速度）

### 3.1 全书表格识别空转 —— 最大的一笔

`engine.py` 在章节循环之前对整本书调用一次 `clean_book_pages`，且传了非空 `cite_key`：

`engine.py` L186–190:

```python
    cleaned_text, all_tables = clean_book_pages(
        doc, cite_key=cite_key, chapter_num=0,
        header_ratio=config.get("header_y_ratio", 0.08),
        footer_ratio=config.get("footer_y_ratio", 0.08),
    )
```

`cleaner.py` 里 `if cite_key:` 就会对**每一页**调用 PyMuPDF 的 `page.find_tables()`——这是整个
PyMuPDF 里最慢的操作之一。而 `all_tables` 在 `engine.py` 中**从未被使用**（全文只有第 186 行这一处）。

实测（12 页 PDF）：

| 操作 | 耗时 | 每页 |
|------|------|------|
| `clean_book_pages(cite_key="TEST")` | 2.16 s | 180 ms |
| `clean_book_pages(cite_key="")` | 0.03 s | 2.7 ms |
| 其中 `find_tables()` 单独 | 2.12 s | 177 ms |
| `get_text("blocks")` 单独 | 0.03 s | 2.5 ms |

即表格识别比取文字慢约 66 倍。推算：

| 书的页数 | 带表格识别 | 不带 |
|----------|-----------|------|
| 500 | 90 s | 1 s |
| 1000 | 180 s | 3 s |
| 3500 | **630 s** | 9 s |

注意 `configs/medical.yaml` 里写的是 `table_extraction: false`——配置以为关掉了，实际
`clean_book_pages` 里的表格识别跟这个开关无关，无条件执行。

而且这段代码在章节循环**之前**，所以：

- `--dry-run`（文档说「只看章节检测，不调 LLM」，本应是秒级）要先等 10 分钟；
- `--resume` 每次续跑都要重新等一遍，而推荐工作流恰恰是「每天跑几小时、Control-C、次日续跑」。

**修法**：`clean_book_pages` 传 `cite_key=""`；更彻底一点——`cleaned_text` 唯一的用途是
`detect_chapters` 的第 2 号降级策略（正则），TOC 能用时它完全没用，可以改成惰性求值：只有 TOC
策略失败才去做全书取文。

### 3.2 Phase 2 双倍翻译

`--fast` 模式下 `split_into_children` 把英文父笔记切成 `_chunks/*.md`。Phase 2 里
`batch_translate.py` 先翻译父笔记正文，然后在同一函数中对每个 chunk 再单独调一次 LLM：

`batch_translate.py` L191–200:

```python
                            for cf_attempt in range(2):
                                try:
                                    cf_resp = ollama.chat(model=MODEL, messages=[
                                        {"role": "user", "content": cf_prompt}
                                    ], options={"temperature": 0.2, "num_ctx": 16384}, keep_alive="0s")
```

chunk 内容之和 ≈ 父笔记正文，所以 Phase 2 的 token 量约为必要量的 2 倍；`child_chunk_size: 4000`
时每章还要多 4–6 次请求（请求数的固定开销比 token 更贵）。

更麻烦的是质量：子文档用的是同一个「写成完整临床笔记」的 stage2 prompt 独立翻译，输出措辞与父笔记
不同。父子文档的设计前提是子文档为父文档的逐字切片，这样才能用子文档命中、用父文档提供上下文。
现在两者对不上，RAG 引用会漂移。

**修法**：翻译完父笔记后，直接对中文父文本重新切分生成子文档，零额外 LLM 调用——省掉约一半
Phase 2 时间，同时恢复父子对齐。

### 3.3 全书文本被提取两遍

`clean_book_pages` 先对全书取一遍文字，之后每章又用 `split_chapter_pages` 取一遍。实测取文字只有
2.5 ms/页，量级不大（3500 页约 9 s），但配合 3.1 的惰性化可以顺手消掉。

真正值得做的是**缓存**：把「PDF → 章节列表 + 每章清洗后文本」的结果落成一份 JSON。191 MB 的
Nelson 文档打开就要 3–5 分钟（README 自己记录的），而这份结果在同一本书上是完全确定的。缓存之后
续跑、`--retry-failed`、`--dry-run` 都从秒级开始。

### 3.4 重试策略在最后一次失败后还要白等 30 秒

`engine.py` L319–321:

```python
                    except Exception as e:
                        print(f"retry{attempt+1}:{e}", end=" ")
                        time.sleep(30)
```

`max_retries: 3` 时，一个注定失败的章节要额外睡 90 s（最后一次 sleep 之后直接进 `else` 分支）。
应该在最后一次尝试后跳过 sleep，并且换成指数退避。

### 3.5 逐章切换模型的路径仍然存在

非 `--fast` 且非 `--bilingual` 时，每章都是 Stage1 → 卸载 → Stage2 → 卸载。646 章意味着约 1300 次
30 GB 级模型装载。文档已经建议改用两阶段工作流，但代码里这条路仍是默认路径。

**修法**：把「按阶段批处理」做进 `engine.py`（例如 `--phase 1` / `--phase 2`），让默认路径就是省的
那条，而不是靠文档提醒用户绕开默认值。

---

## 四、上下文窗口与配置

### 4.1 `process_epub.py` 里超过一半的章节文本被 Ollama 静默丢弃

三个数字对不上：

`process_epub.py` L35:

```python
MAX_CHARS_PER_CHAPTER = 30000
```

`process_epub.py` L383:

```python
                    chapter_text=ch_text[:15000],
```

`process_epub.py` L265:

```python
                options={"temperature": 0.3, "num_ctx": 8192}
```

先按 30000 字符切块，再把每块硬截到 15000 字符（第一次静默丢内容），而 `num_ctx: 8192` 对中文只装
得下大约 5000–8000 字符（Qwen 系分词器中文约 1.5 字/token），于是 prompt 又被 Ollama 截一次。
`postprocess/history.py` 同样问题更明显——`detail_text[:12000]`、`combined[:15000]` 配
`num_ctx: 8192`，代码注释还写着「尽量多送内容给 LLM」，实际送不进去。

**修法**：`num_ctx` 由配置驱动，并按「字符数 → 估算 token 数」反推允许的输入长度；超限时分块，
而不是切断。

### 4.2 `cross_check.py` 的主题聚类会退化

主题 key 是 `##` 小节标题，`len(v) >= 2` 就纳入：

`cross_check.py` L66–67:

```python
    # 只保留在 2+ 本书中出现的主题
    return {k: v for k, v in topics.items() if len(v) >= 2}
```

注释说的是「2+ 本书」，代码判的是「2+ 个小节」——同一本书的两章共用 `## TL;DR` 就会入选。由于
prompt 模板产出的都是通用小节名，排名靠前的「主题」几乎必然是 `TL;DR`、`关键要点` 这类，且
`sources` 会把全部命中小节拼进一个 prompt（Nelson 一本就 646 章 × 1500 字符）。再加上
`list(topics.items())[:50]` 取的是插入顺序而非重要度，产出基本不可用。

**修法**：按 `cite_key` 去重后再要求 ≥ 2 本书；限制每主题来源数（如 4）；过滤通用小节名白名单；
按「跨书数量 × 年份跨度」排序取前 N。

### 4.3 配置里有 7 个键从来没被读过

| 配置键 | 状态 |
|--------|------|
| `trim_references` / `keep_references` | 无人读取；参考文献截断实际上没走到入模文本 |
| `structured_data` / `gen_mermaid` | 无人读取 |
| `incremental_index` | 无人读取 |
| `domain` | 无人读取 |
| `evidence_level`（metadata 字段） | 收集了但不出现在笔记 frontmatter 里 |
| `child_chunk_overlap` | 作为参数传进 `split_into_children`，函数体里完全没用 |

`child_chunk_overlap` 尤其值得一提：README 把父子文档写成 RAG 特性，但**子文档之间没有任何
overlap**，切在小节边界上的语义会被切断。

同时 `chapter_detect.extract_chapter_text()`——那个正确地做了页眉页脚裁剪 + 重复行去重 +
参考文献截断的**按章**清洗函数——全仓库零调用者。第 3.1/2.6 节讲的清洗被绕过，本质上就是这个函数
写好了却没接上，engine 用的是不清洗的 `split_chapter_pages`。

**这是修「清洗被绕过」最省事的路径**：让 `split_chapter_pages` 复用
`extract_chapter_text` 的过滤规则（页眉页脚比例、纯页码、重复行、参考文献），配置项 `header_y_ratio`
/ `footer_y_ratio` / `trim_references` 才算真的生效。

### 4.4 没有进度清单，续跑靠文件大小猜

`--resume` 的判据是「文件存在且 > 2000 字节」，`--retry-failed` 是「< 2000 字节」，2000 这个魔数在
`engine.py` 里硬编码了两处。后果：正常但偏短的章节会被永远重试，而 2.1 KB 的垃圾输出被认为已完成。

**修法**：输出目录里维护一份 `.pipeline_state.json`，记录每章的 status / 用的模型 / 耗时 / 输入文本
hash / 阶段（stage1 done、stage2 done）。续跑、重试、跨阶段衔接、以及 2.4 的重命名问题都能一并解决。

---

## 五、架构与工程卫生

### 5.1 重复代码

| 重复内容 | 份数 | 位置 |
|----------|------|------|
| `slugify()` | 3 | `engine.py`、`process_epub.py`、`batch_translate.py`(`slugify_cn`) |
| frontmatter 解析 `content.index("---", 3)` | 7 | `batch_translate.py`×4、`cross_check.py`、`postprocess/medical.py`、`postprocess/history.py` |
| LLM 调用 + 重试 | 4 套实现 | `engine.py` 内联 4 处（其中 `--bilingual` 那处漏了重试）、`process_epub.py` 的 `call_ollama`、`postprocess/history.py` 的 `query_llm`、`batch_translate.py` 内联 2 处；`cross_check.py`、`postprocess/medical.py` 无重试 |
| `chunk_text_by_paragraphs()` / `split_into_children()` | 2 | `engine.py`、`process_epub.py` |
| 模型名字符串 | 5 个文件 | `batch_translate.py`、`process_epub.py`、`cross_check.py`、`postprocess/*.py`、`cleaner.py`(默认参数) |
| `VAULT_DIR` / `NOTE_FOLDER` | 5 | 同上 |

有 `configs/*.yaml` 这套配置系统，却还要改 5 个文件才能换模型。建议抽一个 `common.py`：
`slugify`、`read_note()/write_note()`（frontmatter 往返）、`llm_call()`（统一重试 + 退避 + 超时 +
num_ctx 计算）、`load_config()`，并让 `batch_translate.py` / `cross_check.py` / `postprocess/*`
也接受 `--config`。

顺带修掉几处：`content.index("---", 3)` 在只有一个 `---` 的文件上抛 `ValueError`，其中
`cross_check.py:36` 和 `postprocess/medical.py:31` 是没有 try 保护的。

### 5.2 `run.sh` 是坏的

```bash
cd /Users/soledadlxw/project/pdf-processor
exec python3 process_book.py "$@"
```

硬编码了别人机器上的绝对路径，而且 `process_book.py` 在仓库里不存在（`.gitignore` 里还留着
`process_book.log`）。要么删掉，要么改成 `cd "$(dirname "$0")" && exec python3 engine.py "$@"`。

### 5.3 仓库卫生

- **`books/` 没有被 gitignore**。`git check-ignore books/test.pdf` 不匹配——`.gitignore` 只写了
  `books_medical/*` 和 `books_history/*`，但 `books/.gitkeep` 是被跟踪的，而 CLAUDE.md 仍然写着
  「确保 PDF 在 `books/` 下」（configs 指向的是 `books_medical/`）。按文档操作就会把几百 MB 的
  PDF 提交进去。
- `pdfs/PipeCheck_...pdf`（896 KB，一篇体系结构论文）被提交进仓库，跟这个流水线没关系。
- `metadata_medical.yaml` 是指向 `metadata.yaml` 的符号链接，configs 引用前者，README 讲后者。
  统一成一个文件名。
- 没有 `requirements.txt`，README 用一行 `pip install pymupdf ollama pyyaml` 代替，且漏了
  `table_extraction: true` 所需的 `camelot`（`cleaner.py` 里是可选 import）。
- 没有测试、没有 CI、没有 linter 配置。`chapter_detect.py` 的降级链和 `cleaner.py` 的清洗规则是
  纯函数，最适合上单元测试；`pdfs/` 里那个 PDF 正好可以当 fixture。
- `python3 -m pyflakes` 报 26 条：7 个未使用 import（`engine.py` 的 `importlib`、`classify_page`；
  `process_epub.py` 的 `json_lib`、`urllib.error`、`OrderedDict`；`postprocess/history.py` 的
  `json`；`cleaner.py` 的 `os`）、2 个未使用局部变量（`engine.py` 的 `total_pages`、
  `attachments_dir`——后者跟第 424 行的 `attach_dir` 是同一个路径，算了两遍）、以及若干
  `f-string is missing placeholders`。

### 5.4 用 `eval()` 执行 YAML 里的表达式

`engine.py` L132–137:

```python
            if eval(check, {"__builtins__": {}}, ns):
                ...
        except Exception:
            pass
```

时效性规则来自配置文件，`eval` 于是成了任意代码执行入口（`__builtins__` 置空只是拦一层，不是
沙箱）。更实际的问题是 `except Exception: pass` 让写错的规则永远静默不触发——现在无法区分
「规则判定为假」和「规则本身语法错误」。

**修法**：换成一个只支持比较和布尔运算的小求值器（或用 `ast.literal_eval` + 白名单操作符），
并在规则报错时打印警告。

### 5.5 全靠 print，没有日志

一次运行可能持续几天、跨多次 Control-C，但输出只在终端。`.gitignore` 里有 `*.log`，却没有任何代码
写日志。建议接 `logging`，加 `--log-file`，把每章的耗时 / 重试次数 / 输入输出字符数记下来——这些
数据也是判断「哪一章输出可疑」的依据。

### 5.6 其他小问题

- `engine.py` 给每篇笔记都打上 `tags: [..., "to-process"]`，且没有任何地方会把它摘掉。
- `--bilingual` 分支靠替换一句中文字面量来改写 prompt
  （`"中文临床笔记（Markdown，不要用代码块包裹）："` → 对照格式），prompt 文件一改就静默失效；
  这条分支还只试 1 次，没有其他分支的 3 次重试。
- `batch_translate.py` 在 import 时就 `open(...).read()` 读 prompt（文件缺失直接崩在 import，
  且文件句柄没关），并且把 medical 的 prompt 写死了，无法用于其他领域。`VAULT_DIR` 定义了没用。
- `translate_note()` 的跳过条件是 `mode in ("full","bilingual") and title_zh`。engine 非 fast 模式
  产出的笔记 `mode == "full"` 但没有 `title_zh`，会被拿去再翻一次（中文翻中文）。
- `postprocess/medical.py` 每篇笔记一次 LLM 调用（约 900 篇），中途失败全部白跑，没有检查点。
- 全仓库 `open()` 都没写 `encoding=`。Python 3.12 在 C locale 下会自动启用 UTF-8 模式，所以
  macOS/Linux 上目前不出问题，但显式写上更稳妥（Windows 默认不是 UTF-8）。

---

## 六、建议的落地顺序

**第一批 — 改动小、收益立刻可见**

1. `clean_book_pages` 调用改传 `cite_key=""`（3.1）：Nelson 规模每次启动省约 10 分钟，`--dry-run`
   回到秒级。
2. 修 `--chapters` 区间解析，空结果时报错退出（2.2）。
3. 重试的最后一次不再 sleep（3.4）。
4. 修 `postprocess/history.py` 的书籍探测 + 三处 `_index.md` 排除条件（2.1）。
5. 删掉或修好 `run.sh`；补 `requirements.txt`；`.gitignore` 加 `books/`；移出 `pdfs/` 里的论文（5.2、5.3）。

**第二批 — 质量与算力，需要小幅重构**

6. 让入模文本走真正的清洗：`split_chapter_pages` 复用 `extract_chapter_text` 的过滤规则（4.3、2.6）。
7. Phase 2 不再单独翻译 chunk，改为切分已翻译的父文本（3.2）：Phase 2 提速约一半，父子文档恢复对齐。
8. prompt 缺失 / 占位符缺失时 fail fast（2.3）。
9. `num_ctx` 配置化 + 按 token 估算切块，取代硬截断（4.1）。
10. 引入 `.pipeline_state.json` 进度清单，替掉「2000 字节」启发式，顺带修好重命名后的续跑（4.4、2.4）。
11. 面包屑改成翻译全部完成后的收尾 pass（2.8）。

**第三批 — 架构**

12. 抽 `common.py`（slugify / frontmatter 往返 / 统一 LLM 调用 / 配置加载），消掉 5 处模型名硬编码（5.1）。
13. 章节文本缓存落盘，让重复运行不再重新解析 191 MB PDF（3.3）。
14. `engine.py` 支持 `--phase 1|2`，把「按阶段批处理」变成默认路径（3.5）。
15. `cross_check.py` 主题聚类按书去重 + 来源数上限 + 重要度排序（4.2）。
16. 给 `chapter_detect.py` / `cleaner.py` 补单元测试（用 `pdfs/` 的 PDF 做 fixture）+ 一条 lint CI（5.3）。
17. 清理死配置键与死代码，或把它们实现掉（`child_chunk_overlap` 建议实现）（4.3、2.5）。
