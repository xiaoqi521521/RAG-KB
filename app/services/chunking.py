import math
import re
from dataclasses import dataclass
from typing import Protocol

from langchain_core.documents import Document
from langchain_text_splitters import RecursiveCharacterTextSplitter

from app.core.config import Settings, get_settings


class ChunkError(Exception):
    """Base exception for document chunking failures."""


class ChunkConfigError(ChunkError):
    """Raised when chunking configuration is invalid."""


@dataclass(frozen=True)
class ChunkConfig:
    """文档分块配置，统一约束 chunk 大小、重叠量和最小保留长度。

    使用不可变 dataclass，避免分块过程中配置被意外修改。
    """

    chunk_size: int = 512
    chunk_overlap: int = 64
    min_chunk_chars: int = 20
    structure_aware: bool = True

    def __post_init__(self) -> None:
        """在配置对象创建后立即校验边界，尽早阻断非法参数。"""
        if self.chunk_size <= 0:
            raise ChunkConfigError("chunk_size must be greater than 0")
        if self.chunk_overlap < 0:
            raise ChunkConfigError("chunk_overlap must be greater than or equal to 0")
        # overlap 不能覆盖整个 chunk，否则切分边界会失去意义。
        if self.chunk_overlap >= self.chunk_size:
            raise ChunkConfigError("chunk_overlap must be less than chunk_size")
        if self.min_chunk_chars < 0:
            raise ChunkConfigError("min_chunk_chars must be greater than or equal to 0")

    @classmethod
    def from_settings(cls, settings: Settings | None = None) -> "ChunkConfig":
        project_settings = settings or get_settings()
        return cls(
            chunk_size=project_settings.rag_chunk_size,
            chunk_overlap=project_settings.rag_chunk_overlap,
        )


class ChunkSplitter(Protocol):
    """分块策略协议，约束不同切分器的统一调用接口。"""

    @property
    def strategy_name(self) -> str:
        ...

    def split(self, doc: Document, config: ChunkConfig) -> list[Document]:
        ...


class RecursiveCharacterChunkSplitter:
    """通用递归字符切分器，按分隔符优先级逐步拆分长文本。"""

    @property
    def strategy_name(self) -> str:
        return "recursive_character"

    def split(self, doc: Document, config: ChunkConfig) -> list[Document]:
        return _split_text_with_recursive_splitter(doc, config, self.strategy_name)


class StructureAwareChunkSplitter:
    """结构感知切分器，优先保留已有章节结构，再对超长内容递归拆分。"""

    @property
    def strategy_name(self) -> str:
        return "structure_aware"

    def split(self, doc: Document, config: ChunkConfig) -> list[Document]:
        # 已经是短小的结构化章节时，不再二次切碎，直接作为一个 chunk 返回。
        if len(doc.page_content) <= config.chunk_size:
            return [
                Document(
                    page_content=doc.page_content,
                    metadata=_build_chunk_metadata(doc, self.strategy_name),
                )
            ]

        return _split_text_with_recursive_splitter(doc, config, self.strategy_name)


class ChunkService:
    """分块服务，根据文档结构选择策略并输出标准化 chunk。"""

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
        """批量切分文档，并补齐统一的 chunk metadata。"""
        chunk_config = config or ChunkConfig.from_settings()
        chunks: list[Document] = []

        # 第一步：逐篇文档选择切分策略，并过滤空文档。
        for doc in docs:
            if not doc.page_content or not doc.page_content.strip():
                continue

            splitter = self._select_splitter(doc, chunk_config)
            # 第二步：执行切分并过滤过短 chunk，避免低信息密度内容进入检索链路。
            for chunk in splitter.split(doc, chunk_config):
                if len(chunk.page_content.strip()) < chunk_config.min_chunk_chars:
                    continue
                chunks.append(_normalize_service_chunk(chunk, splitter.strategy_name))

        # 第三步：为最终输出统一补充顺序索引，便于后续入库和引用定位。
        for index, chunk in enumerate(chunks):
            chunk.metadata["chunk_index"] = index

        return chunks

    def _select_splitter(self, doc: Document, config: ChunkConfig) -> ChunkSplitter:
        # 只有显式开启结构感知，且文档已经带 section_title 时，才认为值得保留章节边界。
        if config.structure_aware and doc.metadata.get("section_title"):
            return self.structure_aware_splitter
        return self.recursive_splitter


def _split_text_with_recursive_splitter(
    doc: Document,
    config: ChunkConfig,
    strategy_name: str,
) -> list[Document]:
    """使用 LangChain 递归切分器按中英文常见分隔符拆分文本。"""
    splitter = RecursiveCharacterTextSplitter(
        chunk_size=config.chunk_size,
        chunk_overlap=config.chunk_overlap,
        # 分隔符从段落到字符逐级退化，优先保持自然语义边界。
        separators=["\n\n", "\n", "。", "！", "？", "；", "，", " ", ""],
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
    """构造 chunk 基础 metadata，只保留分块阶段真正需要的字段。"""
    metadata: dict[str, object] = {
        "split_strategy": strategy_name,
        "estimated_tokens": estimate_tokens(text if text is not None else doc.page_content),
        "page_num": doc.metadata["page_num"],
    }
    if section_title := doc.metadata.get("section_title"):
        metadata["section_title"] = section_title
    return metadata


def _normalize_service_chunk(chunk: Document, strategy_name: str) -> Document:
    """标准化 chunk metadata，避免不同切分器输出字段不一致。"""
    metadata: dict[str, object] = {
        "split_strategy": chunk.metadata.get("split_strategy", strategy_name),
        "estimated_tokens": chunk.metadata.get("estimated_tokens", estimate_tokens(chunk.page_content)),
        "page_num": chunk.metadata["page_num"],
    }
    if section_title := chunk.metadata.get("section_title"):
        metadata["section_title"] = section_title
    return Document(page_content=chunk.page_content, metadata=metadata)


def estimate_tokens(text: str) -> int:
    """粗略估算文本 token 数，用于上下文预算而非精确计费。

    这里对中文和非中文字符使用不同权重，目的是在不调用 tokenizer 的前提下，
    以较低成本得到稳定、可比较的长度估计。
    """
    chinese_chars = len(re.findall(r"[\u4e00-\u9fff]", text))
    non_whitespace_non_chinese_chars = sum(
        1 for char in text if not char.isspace() and not "\u4e00" <= char <= "\u9fff"
    )
    return math.ceil(chinese_chars * 1.5 + non_whitespace_non_chinese_chars * 0.3)
