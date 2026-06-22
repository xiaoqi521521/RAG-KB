from typing import BinaryIO, Protocol

from langchain_core.documents import Document


class DocumentParser(Protocol):
    """文档解析器统一协议，供 DocumentLoaderService 按文件类型分发。"""

    @property
    def supported_types(self) -> set[str]:
        ...

    @property
    def parser_name(self) -> str:
        ...

    def parse(self, file: BinaryIO, file_name: str, file_type: str) -> list[Document]:
        ...


def build_metadata(
    *,
    file_name: str,
    file_type: str,
    page_num: int,
    parser: str,
    section_title: str | None = None,
    title: str | None = None,
) -> dict[str, object]:
    """构造文档加载阶段的标准 metadata，避免各解析器字段不一致。"""
    metadata: dict[str, object] = {
        "source": file_name,
        "file_type": file_type,
        "page_num": page_num,
        "parser": parser,
    }
    if section_title:
        metadata["section_title"] = section_title
    if title:
        metadata["title"] = title
    return metadata
