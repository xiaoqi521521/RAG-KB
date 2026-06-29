# 文档切分 Spec

## 背景与目标

文档加载层已经把 PDF、Word、TXT、Markdown 解析为 `list[langchain_core.documents.Document]`。文档切分层负责把这些较大的 `Document` 拆成适合 Embedding、检索和引用溯源的 chunk。

分块质量直接影响 RAG 链路：

- 块太大：语义被稀释，向量检索精度下降。
- 块太小：上下文不足，生成答案容易片段化。
- 边界切得不好：句子、定义、表格说明可能被截断，影响召回和回答质量。

本项目采用两类策略：

- 结构感知切分：更准确地说是结构保留型切分，消费文档加载阶段已写入的 `section_title`，优先保留章节、标题、页码等结构信息。
- 递归字符切分（带 overlap）：无结构或章节过长时按分隔符递归切分，并用 overlap 保留边界上下文。

## In Scope

- 定义文档切分层统一入口：`ChunkService`。
- 定义切分器统一接口：`ChunkSplitter`，便于后续扩展其它切分策略。
- 输入为文档加载层产出的 `list[Document]`。
- 输出仍为 `list[Document]`，每个 `Document` 表示一个 chunk。
- 支持结构感知切分和递归字符切分（带 overlap）。
- chunk metadata 不继承文档加载阶段的完整 metadata，只保留 `page_num` 和可选的 `section_title`，并追加 chunk 级字段。
- 支持默认参数：`chunk_size=512`、`chunk_overlap=64`、`min_chunk_chars=20`。
- 保留切分层所需的页码字段 `page_num`，以及存在时的章节字段 `section_title`。
- 增加单元测试覆盖策略选择、overlap、metadata 映射、短块过滤和异常参数。

## Out of Scope

- 不调用 Embedding。
- 不写入数据库。
- 不生成 `chunk_id`，数据库入库阶段再生成。
- 不访问 MinIO。
- 不实现向量检索。
- 不实现 Token 预算裁剪，后续上下文裁剪层负责。
- 不引入 LLM 做语义切分。
- 不实现跨文档合并切分。

---

## 输入输出模型

### 输入

输入来自文档加载层：

```python
list[Document]
```

每个输入 `Document` 至少包含：

```plain
page_content
metadata.page_num
```

可能包含：

```plain
metadata.section_title
```

切分层只读取输入 metadata 中的 `page_num` 和 `section_title`。`source`、`file_type` 等其他字段由上游文档记录或后续入库流程维护，不进入切分层输出 metadata。

### 输出

切分层仍返回：

```python
list[Document]
```

输出 `Document.page_content` 为 chunk 正文。

输出 `Document.metadata` 不继承输入文档的完整 metadata，只包含切分层标准字段。其中 `page_num`
是与参考实现 `pageNum` 对齐的主溯源字段，表示 chunk 所属页码或加载层定义的等价位置；切分层必须保留该字段，不能用内部输入序号替代。

输出 `Document.metadata` 字段集合如下：

| metadata key | 必填 | 说明 |
| --- | --- | --- |
| `chunk_index` | 是 | 在当前文档加载结果中的全局 chunk 序号，0-based |
| `split_strategy` | 是 | `structure_aware` 或 `recursive_character` |
| `estimated_tokens` | 是 | 基于字符的粗略 Token 估算 |
| `page_num` | 是 | chunk 所属页码或加载层定义的等价位置 |
| `section_title` | 否 | chunk 所属章节标题；仅输入存在时保留 |

不放入 metadata 的字段：

| 字段 | 原因 |
| --- | --- |
| `chunk_id` | 数据库入库阶段生成，切分层不负责 |
| `embedding` | Embedding 层生成，切分层不负责 |
| `vector_id` | 向量存储层生成，切分层不负责 |
| `score` | 检索阶段才产生 |
| `source` | 文档来源由上游文档记录或入库关联关系维护，不复制到 chunk metadata |
| `file_type` | 文件类型由文档记录维护，切分层输出不携带 |
| `content_length` | 可由 `page_content` 直接计算，不作为 metadata 契约字段 |
| `source_doc_index` | 内部输入序号不适合作为引用溯源字段，使用继承的 `page_num` 对齐 `pageNum` |
| `start_index` | 字符偏移更适合调试或高亮扩展，不作为本阶段输出契约 |

---

## 分块配置

建议配置对象：

```python
class ChunkConfig(BaseModel):
    chunk_size: int = 512
    chunk_overlap: int = 64
    min_chunk_chars: int = 20
    structure_aware: bool = True
```

配置规则：

- `chunk_size` 表示每个 chunk 的目标最大字符数，不是 Token 数。
- `chunk_overlap` 表示相邻 chunk 的重叠字符数，用于保留边界上下文。
- `chunk_overlap` 必须小于 `chunk_size`。
- `min_chunk_chars` 用于过滤过短碎片。
- 参数最终应从配置对象读取，不在业务代码中硬编码。

默认值说明：

| 参数 | 默认值 | 说明 |
| --- | --- | --- |
| `chunk_size` | 512 | 兼顾语义集中度和上下文完整性 |
| `chunk_overlap` | 64 | 约 12.5% 重叠，降低边界信息丢失 |
| `min_chunk_chars` | 20 | 过滤无检索价值的碎片 |
| `structure_aware` | true | 有结构信息时优先使用结构感知切分 |

---

## 策略选择

`ChunkService` 负责统一调度。

策略选择规则：

```plain
输入 list[Document]
  -> 逐个检查 Document
  -> 如果 Document.metadata.section_title 存在，且配置允许结构感知切分
       -> 使用结构感知切分
       -> 输出 chunk metadata.split_strategy="structure_aware"
  -> 否则
       -> 使用递归字符切分（带 overlap）
       -> 输出 chunk metadata.split_strategy="recursive_character"
  -> 过滤过短 chunk
  -> 重新分配全局 chunk_index
  -> 返回 list[Document]
```

说明：

- `structure_aware` 是切分配置开关，不是从输入 `Document.metadata` 读取的字段。
- 当前文档加载层已经把 Markdown 按章节拆分，PDF / Word 也可能从 MinerU 提取 `section_title`。
- 结构感知切分不重新解析 Markdown `#` 标题，也不从 `page_content` 中重新推断章节。
- 对已带 `section_title` 的 `Document`，优先把它视为一个语义单元。
- 如果该语义单元超过 `chunk_size`，仍然需要在章节内部继续切分。
- TXT 通常没有 `section_title`，默认走递归字符切分（带 overlap）。

---

## 递归字符切分（带 overlap）

### 职责

`RecursiveCharacterChunkSplitter` 负责无结构文本或长章节的兜底切分。

它实现 `ChunkSplitter` 接口。

推荐使用 LangChain 的 `RecursiveCharacterTextSplitter` 实现基础切分能力：

```python
from langchain_text_splitters import RecursiveCharacterTextSplitter
```

LangChain 文档推荐 `RecursiveCharacterTextSplitter` 作为通用文本切分器，它会按常见分隔符递归切分，直到 chunk 接近目标大小。

### 默认 splitter 参数

```python
RecursiveCharacterTextSplitter(
    chunk_size=config.chunk_size,
    chunk_overlap=config.chunk_overlap,
    separators=["\n\n", "\n", "。", "！", "？", "；", "，", " ", ""],
)
```

参数意图：

- 优先在段落边界断开。
- 其次在换行、中文句末标点、逗号、空格处断开。
- 最后才按字符硬切。
- 使用 `chunk_overlap` 保留相邻 chunk 的边界上下文。

### 输入输出规则

输入：

```python
Document(page_content="长文本", metadata={...})
```

输出：

```python
list[Document]
```

每个输出 chunk：

- 不继承输入完整 metadata。
- `split_strategy="recursive_character"`。
- 保留输入 metadata 中的 `page_num`，作为与参考实现 `pageNum` 对齐的页码溯源字段。
- 输入 metadata 中存在 `section_title` 时，继续保留该字段；不存在时不写入。
- 写入 `estimated_tokens`。
- 全局 `chunk_index` 由 `ChunkService` 汇总所有切分结果后统一写入。

### 过短文本处理

- 如果输入文本为空或全空白，不产生 chunk。
- 如果切分后 chunk 长度小于 `min_chunk_chars`，默认过滤。
- 如果整个文档本身非空但短于 `min_chunk_chars`，是否保留由 `ChunkService` 统一决定；默认过滤，避免无检索价值碎片入库。

---

## 结构感知切分

### 职责

`StructureAwareChunkSplitter` 负责保留文档加载阶段已经识别出的章节语义边界。它不负责解析 Markdown `#`、Word 标题样式或 MinerU 原始结构；这些结构解析职责属于文档加载层。

它实现 `ChunkSplitter` 接口。

适用输入：

- `Document.metadata["section_title"]` 存在。
- Markdown 加载阶段已经按标题解析出的逻辑章节。
- MinerU / Word / PDF 加载阶段已经提取出章节标题或标题行。

### 切分规则

```plain
输入一个带 section_title 的 Document
  -> 如果正文长度 <= chunk_size
       -> 整个 Document 作为一个 chunk，不再二次切分
  -> 如果正文长度 > chunk_size
       -> 在该章节内部调用递归字符切分（带 overlap）
       -> 子 chunk 继续保留 page_num 和 section_title
```

这样做的目的：

- 小章节保持完整，避免把一个完整语义单元拆碎。
- 输入 `Document` 已经是加载层识别出的逻辑章节时，长度未超限就直接保留为一个 chunk，这是预期行为。
- 大章节不会超出 Embedding 合理长度，仍可通过递归字符切分（带 overlap）兜底。
- 所有子 chunk 都能保留 `page_num` 和 `section_title`，方便引用溯源和结果展示。

### 标题补充规则

如果输入 metadata 没有 `section_title`，结构感知切分器本阶段不再重新解析标题，也不会因为正文中残留 Markdown `#` 而按标题切分。

原因：

- 标题识别已属于文档加载层职责。
- Markdown / MinerU 解析阶段已经尽量补齐 `section_title`。
- 切分层重复解析标题容易造成两套规则不一致。

后续如果真实样本证明加载层标题识别不足，可以单独扩展一个标题识别预处理模块，而不是塞进基础切分逻辑。

---

## Token 估算

切分层需要给每个 chunk 写入 `estimated_tokens`，用于后续粗略评估上下文预算。

本阶段采用轻量估算，不依赖外部 tokenizer：

```plain
中文字符：约 1.5 Token
非空白非中文字符：约 0.3 Token
```

说明：

- 这是保守估算，不保证与具体模型 tokenizer 完全一致。
- 精确 Token 控制应在后续上下文裁剪层实现。
- 估算值只用于监控、调参和预算参考。

---

## 服务接口

建议模块：

```plain
app/services/chunking.py
```

建议类：

```plain
ChunkConfig
ChunkSplitter
ChunkService
RecursiveCharacterChunkSplitter
StructureAwareChunkSplitter
```

切分器接口：

```python
class ChunkSplitter(Protocol):
    @property
    def strategy_name(self) -> str:
        ...

    def split(
        self,
        doc: Document,
        config: ChunkConfig,
    ) -> list[Document]:
        ...
```

接口约束：

- `ChunkSplitter` 只负责把单个输入 `Document` 切成局部 chunk。
- `strategy_name` 必须返回稳定的策略名，并写入输出 chunk 的 `metadata["split_strategy"]`。
- `split(...)` 返回的 chunk 需要保留 `page_num` 和存在时的 `section_title`。
- `split(...)` 不负责全局 `chunk_index` 重排；该职责属于 `ChunkService`。
- `split(...)` 不负责跨文档合并、Embedding、入库或外部服务调用。

建议入口：

```python
class ChunkService:
    def __init__(
        self,
        recursive_splitter: ChunkSplitter,
        structure_aware_splitter: ChunkSplitter,
    ) -> None:
        ...

    def split_documents(
        self,
        docs: list[Document],
        config: ChunkConfig | None = None,
    ) -> list[Document]:
        ...
```

调用约束：

- 成功时返回 `list[Document]`。
- 输入为空时返回空列表。
- 配置非法时抛出明确异常，例如 `ChunkConfigError`。
- 不吞掉不可预期异常，交由上层索引任务服务记录失败。
- `ChunkService` 负责选择具体 `ChunkSplitter`、过滤过短 chunk、重新分配全局 `chunk_index`。

---

## 与数据库入库的衔接

切分层输出的 chunk `Document` 后续会进入 Embedding 和入库流程。

建议入库映射：

| chunk Document | 数据库字段 |
| --- | --- |
| `page_content` | `content` |
| `metadata.page_num` | `page_number` |
| `metadata.section_title` | `section_title` |
| `metadata.chunk_index` | `chunk_index` |
| `metadata.estimated_tokens` | `token_count` 或统计字段 |
| `metadata.split_strategy` | 可观测字段，便于调参 |

切分层不负责：

- 生成数据库主键。
- 生成向量。
- 写入 PGVector。
- 维护索引任务状态。

---

## 错误处理

建议异常：

| 异常 | 说明 |
| --- | --- |
| `ChunkError` | 文档切分失败基类 |
| `ChunkConfigError` | chunk 参数非法 |

参数校验规则：

- `chunk_size > 0`
- `chunk_overlap >= 0`
- `chunk_overlap < chunk_size`
- `min_chunk_chars >= 0`

文本处理规则：

- 空输入列表返回空列表。
- 空白 `page_content` 跳过。
- 全部输入都为空时返回空列表。

---

## 验收标准

### 场景一：TXT 走递归字符切分（带 overlap）

GIVEN 一个没有 `section_title` 的长文本 `Document`

WHEN 调用 `ChunkService.split_documents(...)`

THEN 返回多个 chunk，metadata 中 `split_strategy="recursive_character"`，每个 chunk 保留 `page_num`，不复制 `source`、`file_type`。

### 场景二：短章节保持完整

GIVEN 一个带 `section_title` 且正文长度小于 `chunk_size` 的 `Document`

WHEN 调用切分服务

THEN 返回 1 个 chunk，正文不被拆分，metadata 中保留原 `section_title`。

### 场景三：长章节内部递归字符切分

GIVEN 一个带 `section_title` 且正文长度大于 `chunk_size` 的 `Document`

WHEN 调用切分服务

THEN 返回多个 chunk，每个 chunk 都保留同一个 `section_title` 和 `page_num`，并带有 `chunk_index`。

### 场景四：overlap 生效

GIVEN `chunk_size=100`、`chunk_overlap=20` 的长文本

WHEN 使用递归字符切分（带 overlap）

THEN 相邻 chunk 在边界附近存在重叠内容。

### 场景五：短碎片过滤

GIVEN 切分结果中存在长度小于 `min_chunk_chars` 的碎片

WHEN `ChunkService` 汇总结果

THEN 这些短碎片不会出现在最终返回列表中。

### 场景六：非法参数

GIVEN `chunk_overlap >= chunk_size`

WHEN 构造配置或调用切分服务

THEN 抛出 `ChunkConfigError` 或等价参数异常。

### 场景七：metadata 可追溯

GIVEN 输入 Document metadata 至少包含 `page_num`，并可能包含 `section_title`

WHEN 完成切分

THEN 每个输出 chunk 的 metadata 只包含 `chunk_index`、`split_strategy`、`estimated_tokens`、`page_num`，以及输入存在时保留的 `section_title`。

### 场景八：切分器接口可扩展

GIVEN 新增一个实现 `ChunkSplitter` 的切分器

WHEN `ChunkService` 选择并调用该切分器

THEN 输出仍然满足统一 chunk `Document` 契约，并由 `ChunkService` 统一过滤短块、重排全局 `chunk_index`。

---

## 技术约束

- 使用 Python 3.12。
- 使用 LangChain `Document` 作为输入和输出。
- 通用切分优先使用 `langchain_text_splitters.RecursiveCharacterTextSplitter`。
- 具体切分策略通过 `ChunkSplitter` 接口扩展。
- 不引入 LLM 或外部语义服务做分块。
- chunk 参数必须可配置。
- 分块结果 metadata 只包含 `chunk_index`、`split_strategy`、`estimated_tokens`、`page_num`，以及输入存在时保留的 `section_title`。
- 不在切分层写数据库、调用 Embedding 或访问外部存储。
- 测试优先覆盖真实行为，少 mock 内部实现。

## 风险与取舍

- 使用字符数而不是精确 Token 数，简单稳定，但与具体模型 tokenizer 存在误差。
- `RecursiveCharacterTextSplitter` 能兼顾常见边界，但不能理解所有复杂表格或代码结构。
- 短章节直接保留可以最大化语义完整性，但可能产生较短 chunk；通过 `min_chunk_chars` 控制碎片。
- 长章节内部切分会保留 `section_title`，但 chunk 本身可能只覆盖章节的一部分，引用展示时需要同时显示页码和章节。
- 后续可通过 RAGAS、Hit Rate、MRR 等评估指标调整 `chunk_size`、`chunk_overlap` 和分隔符顺序。
