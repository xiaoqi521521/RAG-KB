# 文档加载层多格式解析 Spec

## 背景与目标

离线索引管道的第一步是把用户上传的文档解析成可分块、可向量化、可追溯来源的结构化文本。原 Spring AI 参考项目通过 `ParseResult` 和 `PageContent` 表达一次解析结果，其中 `PageContent.text`、`pageNum`、`sectionTitle` 是后续分块和引用溯源真正需要的核心信息。

LangChain 重构版不再新增项目内部 `ParseResult` 模型，而是直接使用 LangChain 的 `Document` 对象承载文档加载结果：

- `Document.page_content` 对齐原 `PageContent.text`。
- `Document.metadata["page_num"]` 对齐原 `PageContent.pageNum`。
- `Document.metadata["section_title"]` 对齐原 `PageContent.sectionTitle`。

这样可以减少中间模型转换，同时保持与 LangChain splitter、embedding、vector store 的接口天然衔接。

## In Scope

- 定义统一解析器协议：
  - `DocumentParser`
- 实现三类解析器：
  - `PdfParser` / `WordParser`：负责复杂文档，复用 `MinerULoaderClient` 的 SDK 调用能力。
  - `TxtParser`：负责 TXT。
  - `MarkdownParser`：负责 Markdown / MD。
- 实现统一入口：
  - `DocumentLoaderService`
- 统一返回 `list[langchain_core.documents.Document]`。
- 设计文档加载阶段的 `Document.metadata` 字段。
- 支持解析失败时抛出项目内可捕获异常，便于索引任务服务记录失败状态。
- 增加单元测试覆盖文件类型分发、成功解析、失败解析和 `Document.metadata` 字段。

## Out of Scope

- 自研 OCR。
- 自研 PDF 复杂版面解析。
- 自研 DOCX / PPTX / XLSX 深度解析。
- MinIO 文件下载。
- 文档上传 API。
- 分块策略实现。
- Embedding、向量入库、索引任务状态更新。
- 前端展示解析结果。

---

## 解析策略

### 总体分发

`DocumentLoaderService` 根据文件扩展名选择解析器：

| 文件类型 | 解析器 | 说明 |
| --- | --- | --- |
| PDF | MinerU 官方 LangChain SDK | 复杂版面、扫描件、表格、公式等交给 MinerU |
| DOC / DOCX | MinerU 官方 LangChain SDK | 使用 MinerU 的 Word 文档解析能力 |
| TXT | TxtParser | 轻量文本解析 |
| MD / Markdown | MarkdownParser | 轻量 Markdown 解析 |

### 为什么 TXT / Markdown 不走 MinerU

TXT 和 Markdown 已经是文本或半结构化文本，不需要 OCR、版面分析、表格识别或公式识别。强行交给 MinerU 会增加部署成本、解析耗时和失败面，也可能引入不必要的格式转换损耗。

本项目对上层保持统一入口，而不是要求所有底层解析都走同一个引擎。

### 为什么 TXT / Markdown 不使用 langchain_community loader

TXT / Markdown 的项目需求很轻：读取文本、按固定规则清洗或切分，并直接产出带项目 metadata 的 `Document`。如果改用 `langchain_community.document_loaders`，仍然需要额外适配层补齐 `file_type`、`page_num`、`section_title` 等字段，整体复杂度没有明显降低。

因此本阶段保留项目内轻量自定义解析器：

- `TxtParser` 负责编码识别、换行规范化、空文本和疑似二进制文件判断。
- `MarkdownParser` 负责按一级/二级标题切分逻辑章节、避开代码块标题误判，并按参考项目规则合并过短章节。
- 两个解析器都直接返回符合项目 metadata 规范的 `Document`，不再引入额外 loader adapter。

---

## 输出模型

### LangChain Document

文档加载层统一返回 `list[Document]`。列表中的每个 `Document` 表示一个可追溯的文本单元，可以是一页、一个逻辑章节、一个 sheet、一个图片 OCR 结果或 MinerU 识别出的版面块。

```python
Document(
    page_content="当前页或章节文本",
    metadata={
        "source": "员工手册.pdf",
        "file_type": "PDF",
        "page_num": 3,
        "section_title": "考勤制度",
    },
)
```

字段对齐关系：

| LangChain 字段 | 对齐原参考项目 | 说明 |
| --- | --- | --- |
| `page_content` | `PageContent.text` | 当前页、章节或版面块的正文 |
| `metadata["page_num"]` | `PageContent.pageNum` | 1-based 页码或逻辑页序号 |
| `metadata["section_title"]` | `PageContent.sectionTitle` | 当前文本单元所属章节标题，可为空 |

### Metadata 字段

文档加载阶段的 metadata 保持轻量、可序列化、可继承给分块阶段。

| metadata key | 必填 | 说明 |
| --- | --- | --- |
| `source` | 是 | 原始文件名或来源标识 |
| `file_type` | 是 | `DocumentLoaderService` 识别出的文件类型，例如 `PDF`、`TXT`、`MD` |
| `page_num` | 是 | 1-based 页码或逻辑页序号 |
| `section_title` | 否 | 章节标题，可为空 |

不放入 metadata 的字段：

| 字段 | 原因 |
| --- | --- |
| `success` | 这是一次解析调用的状态，不是文本单元元数据 |
| `error_msg` | 失败原因应由异常或索引任务状态记录 |
| `pages` | 已由 `list[Document]` 自然表达 |
| `raw_mineru_result` | 体积大且会把外部格式泄漏到业务层 |

规则：

- MinerU 解析 PDF / 图片时，尽量保留真实页码。
- MinerU 解析 Office 文档时，如果无法获得真实页码，可使用逻辑页序号。
- TXT 默认作为 1 个逻辑页，`page_num=1`。
- Markdown 可以按标题切分为多个逻辑章节，`page_num` 使用逻辑章节序号；短章节会继续累积到后续章节，避免产生过短 `Document`。
- 空白页或空白章节不进入返回列表。
- 返回的每个 `Document.page_content` 必须非空。

---

## 解析器协议

所有解析器实现统一协议：

```python
class DocumentParser(Protocol):
    @property
    def supported_types(self) -> set[str]:
        ...

    def parse(self, file: BinaryIO, file_name: str, file_type: str) -> list[Document]:
        ...
```

设计约束：

- `supported_types` 使用大写扩展名，例如 `{"PDF", "DOCX"}`、`{"MD", "MARKDOWN"}`。
- `parse()` 成功时返回非空 `list[Document]`。
- `parse()` 失败时抛出 `DocumentParseError` 或其子类。
- 解析器不写数据库、不访问 MinIO、不调用 Embedding。
- 解析器返回的 `Document` 必须包含完整文档加载 metadata。

---

## 错误处理

文档加载层不再用 `ParseResult.success=false` 表示失败。失败通过项目内异常表达，由上层索引任务服务捕获并记录状态。

建议异常：

| 异常 | 说明 |
| --- | --- |
| `DocumentParseError` | 文档解析失败的基类 |
| `UnsupportedFileTypeError` | 不支持的文件类型 |
| `EmptyDocumentError` | 解析后没有可用正文 |
| `ExternalParserError` | MinerU 等外部解析服务调用失败 |

错误处理规则：

- 不支持的文件类型：抛出 `UnsupportedFileTypeError`。
- 解析后没有可用正文：抛出 `EmptyDocumentError`。
- MinerU 超时、返回错误或输出不可识别：抛出 `ExternalParserError`，错误信息保留简短可排查摘要。
- 解析层只负责抛出异常和记录必要日志，不负责更新索引任务表。

---

## MinerU 解析器

### 职责

`MinerULoaderClient` 负责调用 MinerU 官方 `langchain-mineru` SDK。`PdfParser` 负责 PDF 页级结果转换，`WordParser` 负责 Word 的 Markdown 逻辑章节转换。

支持类型：

- PDF
- DOC
- DOCX

本阶段先实现 PDF 和 Word 加载。`MinerULoader` 返回的 `page_content` 为 Markdown，本项目会进一步收敛为项目内部统一 metadata。Word 返回内容会按参考项目的标题阈值规则再拆成逻辑章节。

### 集成方式

第一阶段优先使用 MinerU 官方 `langchain-mineru` SDK，并使用 `precision` 模式，适合正式离线索引和入库。

计划配置：

```env
MINERU_ENABLED=true
MINERU_MODE=sdk
MINERU_API_TYPE=precision
MINERU_API_URL=
MINERU_TIMEOUT_SECONDS=300
```

说明：

- `MINERU_ENABLED`：是否启用 MinerU 解析复杂文档。
- `MINERU_MODE`：MinerU 集成方式，本阶段默认 `sdk`。
- `MINERU_API_TYPE`：MinerU SDK 解析模式，本阶段默认 `precision`，也兼容 `flash`。
- `MINERU_API_URL`：官方 SDK 默认不需要 URL，保留为空；如后续接入自部署服务再使用。
- `MINERU_TIMEOUT_SECONDS`：单文件解析超时时间。

如果 `MINERU_ENABLED=false`，复杂文档解析应返回明确失败，不自动退回低质量解析器，避免用户误以为复杂文档已高质量入库。

### 输出转换规则

MinerU SDK 返回结果转换为项目 `Document` 时需要遵守：

- 正文内容进入 `Document.page_content`。
- 页码或逻辑顺序进入 `metadata["page_num"]`。
- 标题层级中最接近当前段落的标题进入 `metadata["section_title"]`。
- Word 使用 MinerU 输出的 Markdown，再按一级/二级标题切分；遇到新标题时，当前累计章节超过 200 字才收束为一个 `Document`。
- 没有可用正文时抛出 `EmptyDocumentError`。

---

## TXT 解析器

`TxtParser` 负责项目内轻量自定义文本解析，不使用 `langchain_community.document_loaders.TextLoader`。

规则：

- 先尝试 UTF-8。
- 如果替换字符比例过高，降级尝试 GBK。
- 去除 UTF-8 BOM。
- 统一换行为 `\n`。
- 空文本抛出 `EmptyDocumentError`。
- 非可打印控制字符比例过高时，判定为疑似二进制文件并抛出 `DocumentParseError`。

TXT 解析结果通常只有一个 `Document`：

- `page_content` 为全文。
- `metadata["page_num"]=1`。

---

## Markdown 解析器

`MarkdownParser` 负责项目内轻量自定义 Markdown 解析，不使用 `langchain_community.document_loaders`。

规则：

- 支持 `.md` 和 `.markdown`。
- 按一级、二级标题拆分逻辑章节。
- 代码块内的 `#` 不识别为标题。
- 遇到新标题时，当前累计章节超过 100 字才收束为一个 `Document`；不足 100 字则继续累积到后续章节。
- 如果短章节合并到后续章节，`section_title` 采用最后遇到的一级/二级标题，保持与参考项目简单规则一致。
- 链接保留可见文字。
- 图片替换为 `[图片]` 标记。
- 代码块替换为 `[代码块]` 标记。
- 没有标题时，整篇文档作为一个逻辑页。

Markdown 解析结果：

- 每个满足最小章节长度阈值的逻辑章节返回一个 `Document`。
- `metadata["page_num"]` 为逻辑章节序号。
- `metadata["section_title"]` 为当前章节标题，可为空。

---

## 统一入口

`DocumentLoaderService` 负责：

- 根据文件名扩展名识别文件类型。
- 根据类型分发到对应解析器。
- 记录解析耗时和结果摘要日志。
- 对不支持的文件类型抛出明确异常。
- 返回解析器生成的 `list[Document]`。

建议入口：

```python
class DocumentLoaderService:
    def load(self, file: BinaryIO, file_name: str) -> list[Document]:
        ...
```

返回规则：

- 成功时返回非空 `list[Document]`。
- 失败时抛出项目内文档解析异常。
- 不返回 `success/error_msg/pages` 包装对象。

---

## 文件结构

计划新增：

```plain
app/services/document_loader/
  __init__.py
  exceptions.py
  parsers.py
  mineru_client.py
  pdf_parser.py
  word_parser.py
  markdown_parser.py
  txt_parser.py
  service.py

tests/services/
  test_document_loader.py
```

计划复用：

```plain
tests/resources/test-docs/
  hr-handbook.txt
  tech-spec.txt
  product-faq.txt
```

---

## 验收标准

### 场景一：解析 TXT

GIVEN `tests/resources/test-docs/hr-handbook.txt`

WHEN 调用 `DocumentLoaderService.load(...)`

THEN 返回非空 `list[Document]`，第一个 `Document.page_content` 包含“员工手册”，metadata 包含 `source`、`file_type`、`page_num`。

### 场景二：解析 Markdown

GIVEN 包含一级、二级标题和代码块的 Markdown 文本

WHEN 调用 Markdown 解析器

THEN 代码块内的 `#` 不被误判为标题，返回的章节标题来自真实 Markdown 标题。

### 场景二补充：Markdown 短章节合并

GIVEN 连续两个一级或二级标题，前一个标题下内容不足 100 字

WHEN 调用 Markdown 解析器

THEN 前一个短章节不会单独返回 `Document`，而是继续累积到后续章节；最终 `section_title` 使用最后遇到的标题。

### 场景三：复杂文档分发到 MinerU

GIVEN 文件名 `policy.pdf`

WHEN 调用 `DocumentLoaderService.load(...)`

THEN PDF 被分发给 `PdfParser`，DOC / DOCX 被分发给 `WordParser`。

### 场景四：MinerU 输出转换为 Document

GIVEN MinerU 返回可识别的 Markdown 或 JSON 输出

WHEN `PdfParser` 或 `WordParser` 转换结果

THEN 返回非空 `list[Document]`，每个 `Document` 都包含非空 `page_content` 和完整文档加载 metadata。

### 场景五：MinerU 失败

GIVEN MinerU 超时、返回错误或输出为空

WHEN 调用 `PdfParser.parse(...)` 或 `WordParser.parse(...)`

THEN 抛出 `ExternalParserError` 或 `EmptyDocumentError`，错误信息包含可排查的失败原因。

### 场景六：不支持的文件类型

GIVEN 文件名 `archive.zip`

WHEN 调用 `DocumentLoaderService.load(...)`

THEN 抛出 `UnsupportedFileTypeError`，错误信息包含“不支持的文件类型”。

### 场景七：metadata 与参考模型对齐

GIVEN 一个包含标题、页码和章节标题的解析结果

WHEN 转换为 LangChain `Document`

THEN `page_content` 对齐原 `PageContent.text`，`page_num` 对齐原 `PageContent.pageNum`，`section_title` 对齐原 `PageContent.sectionTitle`。

---

## 技术约束

- 使用 Python 3.12。
- 统一使用 LangChain `Document` 表达文档加载结果。
- TXT / Markdown 使用项目内轻量自定义解析器，不引入 `langchain_community` loader adapter。
- TXT / Markdown 解析不依赖 MinerU。
- PDF / Word 解析依赖 MinerU 官方 `langchain-mineru` SDK，但 SDK 输出必须转换为项目 metadata 规范。
- 不在解析层调用 LLM。
- 不在解析层写数据库。
- 不在解析层访问 MinIO。
- 不把 MinerU 输出格式作为项目内部标准。
- 测试优先使用内存文件、伪造 MinerU 输出和 `tests/resources/test-docs/`，避免依赖真实外部服务。

## 风险与取舍

- 直接返回 `list[Document]` 可以减少中间模型，但失败状态不能再依赖返回值表达，需要上层捕获异常并更新索引任务。
- MinerU 能显著提升复杂文档解析质量，但部署和运行成本高于轻量解析器。
- MinerU 官方 SDK 输出格式需要适配层，后续 `langchain-mineru` 版本升级时可能需要调整适配代码。
- TXT / Markdown 不走 MinerU，可以减少不必要的依赖、耗时和失败面。
- TXT / Markdown 不使用 `langchain_community` loader，可以避免为了补齐项目 metadata 再增加一层 adapter；代价是需要维护少量清洗和标题切分逻辑。
- 本阶段只定义文档加载层，不处理上传、存储、分块和入库。
