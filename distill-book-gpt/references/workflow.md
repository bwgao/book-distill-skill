# 文件与执行约定

## 目录
1. 命令
2. 元数据
3. 切分计划
4. 卡片数据
5. 状态与输入限制

## 命令

设置 `SKILL_DIR` 为当前技能目录绝对路径。优先使用 `CODEX_PRIMARY_RUNTIME_PYTHON`，否则 `python3`。脚本需要 PyYAML、Pandoc；PDF 另需 PyMuPDF。先检查实际依赖，不假设环境有包或网络；缺少时使用环境可用的等价工具或说明阻塞，不用简陋提取器伪装成功。

```bash
python3 "$SKILL_DIR/scripts/book_pipeline.py" prepare /absolute/book.epub --root /absolute/work
python3 "$SKILL_DIR/scripts/book_pipeline.py" split /absolute/book-dir --plan /absolute/book-dir/99-raw/split-plan.json
python3 "$SKILL_DIR/scripts/book_pipeline.py" export-source /absolute/book-dir
python3 "$SKILL_DIR/scripts/book_pipeline.py" stamp /absolute/book-dir --stage book-summary --reviewed
python3 "$SKILL_DIR/scripts/publish.py" mind /absolute/book-dir
python3 "$SKILL_DIR/scripts/book_pipeline.py" stamp /absolute/book-dir --stage mind --reviewed
python3 "$SKILL_DIR/scripts/book_pipeline.py" stamp /absolute/book-dir --stage chapter-summary --id ch01 --reviewed
python3 "$SKILL_DIR/scripts/book_pipeline.py" skip /absolute/book-dir --id frontmatter01 --reason '只有版权信息，已保留原文'
python3 "$SKILL_DIR/scripts/publish.py" book /absolute/book-dir --slug book-reading
python3 "$SKILL_DIR/scripts/publish.py" check /absolute/book-dir
```

`prepare` 只提取并提出候选，不把标题候选直接当最终章。读取 `99-raw/units.json`、`document.json`（EPUB）、`navigation.json`、`extraction-report.json` 和目录候选后自主核验。必要时修复 raw 块和图片并记录改动，再拆分。

不覆盖已有目录。中断后使用已有项目，不重复 prepare。需要修复拆分时先保留原 manifest/摘要，核对影响范围；不要绕过脚本的防覆盖检查。新版本可建立新目录，并把未变且输入指纹一致的输出迁移过去。`stamp` 是审核后记录工具，不代替阅读原文。

## 元数据

`book-meta.md` 使用以下 YAML frontmatter。其后写简短字段来源，标出 OPF 与版权页冲突及取舍。

```yaml
---
title_zh: null
title_original: null
author: []
translator: []
publisher: null
imprint: null
edition: null
publication_year: null
publication_month: null
isbn: null
source_language: null
output_language: zh-CN
book_type: null
source_format: EPUB
original_publication_year: null
source_file: uploaded-book.epub
---
```

依据原书判断 `book_type`，用简短描述性标识，例如 investment-general；不要把样例书分类套用所有书。PDF 的 source_format 写 PDF。草稿可能只提取少数字段；全部字段保留，查不到则未知，绝不自动继承样例值。

## 切分计划

单位采用从零开始的 `units.json` 索引。EPUB 单位为保留格式的 Pandoc 块；PDF 单位为带物理页码和坐标的文本/图片块。以相邻开始位置自动推导半开区间 `[start, end)`。首项必须从 0 开始；最后一项自动延伸至末尾，因此每个原文块恰好归属一次。

```json
{
  "source_sha256": "从 manifest 复制真实值",
  "reviewed": true,
  "extraction_reviewed": true,
  "chapters": [
    {"title": "版权与扉页", "kind": "frontmatter", "start": 0},
    {"title": "序", "kind": "preface", "start": 4},
    {"title": "第一章 章节标题", "kind": "chapter", "start": 9},
    {"title": "后记", "kind": "afterword", "start": 72}
  ]
}
```

允许 kind：chapter、preface、afterword、appendix、part、toc、frontmatter、index、other。正文按出现顺序 ch01/ch02；其他部分按类型独立编号。不要把“第一部”错当章，也不要因大小标题字号一致而把节当章。若一部前言与下一章在同一文件，按原文内容边界拆开。

扫描 PDF 有非空 `ocr_required_pages` 时，split 会停止；完成 OCR 后修订 units，并在 extraction-report 增加 `ocr_reviewed: true`，记录 OCR 工具及逐页检查。只有真实完成识别和校对后才能置 true。页码为物理页，印刷页码单独说明。

## 卡片数据

写 `99-raw/mind.json`，所有字段是纯文本（不要写 HTML/Markdown）：

```json
{
  "title": "书名",
  "subtitle": "由原文归纳的导语",
  "core": "全书主旨",
  "cards": [
    {"title": "主题名", "points": [{"label": "要点名", "text": "要点解释"}]}
  ],
  "logic_title": "全书论证链",
  "logic": ["论证起点", "支撑过程", "结论"],
  "takeaway": "一句话记忆"
}
```

卡片及要点数量由书本决定。`logic` 表示论证先后，不是必须六步；并列内容用文字明确关系，必要时将 logic_title 改为“全书阅读路径”。书籍标题和摘要从真实输入提取；示例 schema 不提供任何图书知识。

## 状态与输入限制

manifest 记录来源 SHA256、原始文件名、阶段状态、按目录排序的章、输入/输出指纹及跳过理由。输入指纹变化后重新生成受影响输出：全书总结依赖全部章节原文与原书；章节摘要依赖该章和 book-meta；思维导图依赖原文与全书总结；出版物依赖全部已核验产物。

只有审核通过后运行 stamp。该命令计算哈希并记录 reviewed；它不能判断内容语义正确。渲染会拒绝未审核、缺失或已陈旧的产物。不要为通过检查虚标状态。

多个章节代理可并行生成，各自只写指定摘要，父代理统一更新 manifest，避免并发覆盖。输入/输出路径、执行语言和保存要求可作为调度信息；不得在用户的固定内容提示词后附加新的拆书方法或案例。

全书和章节代理读到的材料包含原文内插图、表格及必要脚注。若脚注被提取到书末，按真实引用把对应原文附入章节原文并记录位置，不加入自己的解释。不要让章节代理自行读取其他章“补齐”。所有引用的图片应先打开检查，再描述其含义；看不清则标明，而不猜数字。
