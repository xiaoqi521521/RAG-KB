import math
import re
from typing import Protocol

from langchain_core.documents import Document
from langchain_text_splitters import RecursiveCharacterTextSplitter
from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class ChunkError(Exception):
    """文档分块模块的基础异常，供上层统一识别分块链路失败。"""


class ChunkConfig(BaseSettings):
    """文档分块配置，直接从环境变量或 .env 文件读取并由 Pydantic 校验。

    Args:
        chunk_size: 单个 chunk 的最大字符数。
        chunk_overlap: 相邻 chunk 之间保留的重叠字符数。
        min_chunk_chars: 过滤低信息量短 chunk 的最小字符数。
        structure_aware: 是否优先保留已解析出的章节边界。
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        env_prefix="RAG_",
        env_ignore_empty=True,
        case_sensitive=False,
        extra="ignore",
        frozen=True,
        populate_by_name=True,
    )

    chunk_size: int = Field(default=512, gt=0, validation_alias="RAG_CHUNK_SIZE")
    chunk_overlap: int = Field(default=64, ge=0, validation_alias="RAG_CHUNK_OVERLAP")
    min_chunk_chars: int = Field(default=20, ge=0, validation_alias="RAG_MIN_CHUNK_CHARS")
    structure_aware: bool = Field(default=True, validation_alias="RAG_STRUCTURE_AWARE")

    @model_validator(mode="after")
    def validate_bounds(self) -> "ChunkConfig":
        """校验跨字段约束，返回可用于分块服务的合法配置。"""
        if self.chunk_overlap >= self.chunk_size:
            # overlap 覆盖整个 chunk 时，滑动窗口无法向前推进，必须交给 Pydantic 统一报参。
            raise ValueError("chunk_overlap must be less than chunk_size")
        return self


class ChunkSplitter(Protocol):
    """分块策略协议，约束所有 splitter 必须暴露策略名和统一 split 接口。"""

    @property
    def strategy_name(self) -> str:
        ...

    def split(self, doc: Document, config: ChunkConfig) -> list[Document]:
        ...


class RecursiveCharacterChunkSplitter:
    """通用递归字符分块器，适合没有章节结构的普通文本。

    Args:
        doc: LangChain Document，包含正文和来源 metadata。
        config: 分块大小、重叠量等运行配置。
    Returns:
        带标准化 metadata 的 Document chunk 列表。
    """

    @property
    def strategy_name(self) -> str:
        return "recursive_character"

    def split(self, doc: Document, config: ChunkConfig) -> list[Document]:
        return _split_text_with_recursive_splitter(doc, config, self.strategy_name)


class StructureAwareChunkSplitter:
    """结构感知分块器，优先保留 document loader 解析出的章节边界。"""

    @property
    def strategy_name(self) -> str:
        return "structure_aware"

    def split(self, doc: Document, config: ChunkConfig) -> list[Document]:
        """按章节结构切分文档，短章节直接作为一个 chunk 返回。"""
        if len(doc.page_content) <= config.chunk_size:
            # 已经小于 chunk 上限的章节不再二次切碎，方便后续引用溯源保留章节语义。
            return [
                Document(
                    page_content=doc.page_content,
                    metadata=_build_chunk_metadata(doc, self.strategy_name),
                )
            ]

        return _split_text_with_recursive_splitter(doc, config, self.strategy_name)


class ChunkService:
    """批量分块服务，根据文档结构选择策略并输出统一 chunk metadata。"""

    def __init__(
        self,
        recursive_splitter: ChunkSplitter | None = None,
        structure_aware_splitter: ChunkSplitter | None = None,
    ) -> None:
        self.recursive_splitter = recursive_splitter or RecursiveCharacterChunkSplitter()
        self.structure_aware_splitter = structure_aware_splitter or StructureAwareChunkSplitter()

    def split_documents(
        self,
        docs: list[Document],
        config: ChunkConfig | None = None,
    ) -> list[Document]:
        """切分文档列表并返回全局顺序稳定的 chunk 列表。"""
        chunk_config = config or ChunkConfig()
        chunks: list[Document] = []

        # 第一步：过滤空文档，并根据章节信息选择普通分块或结构感知分块。
        for doc in docs:
            if not doc.page_content or not doc.page_content.strip():
                continue

            splitter = self._select_splitter(doc, chunk_config)
            # 第二步：过滤过短 chunk，避免低信息密度内容进入索引和检索链路。
            for chunk in splitter.split(doc, chunk_config):
                if len(chunk.page_content.strip()) < chunk_config.min_chunk_chars:
                    continue
                chunks.append(_normalize_service_chunk(chunk, splitter.strategy_name))

        # 第三步：重新分配全局 chunk_index，保证过滤后仍能稳定追踪顺序。
        for index, chunk in enumerate(chunks):
            chunk.metadata["chunk_index"] = index

        return chunks

    def _select_splitter(self, doc: Document, config: ChunkConfig) -> ChunkSplitter:
        """根据配置和章节 metadata 选择分块策略。"""
        if config.structure_aware and doc.metadata.get("section_title"):
            # 只有 loader 明确提供章节标题时才走结构感知，避免对普通文本误判结构。
            return self.structure_aware_splitter
        return self.recursive_splitter


def _split_text_with_recursive_splitter(
    doc: Document,
    config: ChunkConfig,
    strategy_name: str,
) -> list[Document]:
    """调用 LangChain 递归字符切分器，并补充分块阶段所需 metadata。"""
    splitter = RecursiveCharacterTextSplitter(
        chunk_size=config.chunk_size,
        chunk_overlap=config.chunk_overlap,
        # 分隔符从段落到标点逐级退化，尽量保留自然语义边界。
        separators=["\n\n", "\n", "。", "；", "，", "：", " ", ""],
    )
    return [
        Document(
            page_content=text,
            metadata=_build_chunk_metadata(doc, strategy_name, text),
        )
        for text in splitter.split_text(doc.page_content)
        if text.strip()
    ]


def _build_chunk_metadata(
    doc: Document,
    strategy_name: str,
    text: str | None = None,
) -> dict[str, object]:
    """构造 chunk 基础 metadata，返回引用溯源和索引入库所需字段。"""
    metadata: dict[str, object] = {
        "split_strategy": strategy_name,
        "estimated_tokens": estimate_tokens(text if text is not None else doc.page_content),
        "page_num": doc.metadata["page_num"],
    }
    if section_title := doc.metadata.get("section_title"):
        metadata["section_title"] = section_title
    return metadata


def _normalize_service_chunk(chunk: Document, strategy_name: str) -> Document:
    """统一不同 splitter 的输出字段，避免下游依赖各策略的内部 metadata 形态。"""
    metadata: dict[str, object] = {
        "split_strategy": chunk.metadata.get("split_strategy", strategy_name),
        "estimated_tokens": chunk.metadata.get(
            "estimated_tokens",
            estimate_tokens(chunk.page_content),
        ),
        "page_num": chunk.metadata["page_num"],
    }
    if section_title := chunk.metadata.get("section_title"):
        metadata["section_title"] = section_title
    return Document(page_content=chunk.page_content, metadata=metadata)


def estimate_tokens(text: str) -> int:
    """粗略估算文本 token 数，返回用于预算裁剪的稳定近似值。"""
    # 中文字符和非中文字符采用不同权重，避免在没有 tokenizer 时明显低估中文内容。
    chinese_chars = len(re.findall(r"[\u4e00-\u9fff]", text))
    non_whitespace_non_chinese_chars = sum(
        1 for char in text if not char.isspace() and not "\u4e00" <= char <= "\u9fff"
    )
    return math.ceil(chinese_chars * 1.5 + non_whitespace_non_chinese_chars * 0.3)
