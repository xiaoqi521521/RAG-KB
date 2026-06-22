from pathlib import Path
from typing import BinaryIO

from langchain_core.documents import Document

from app.services.document_loader.exceptions import UnsupportedFileTypeError
from app.services.document_loader.parsers import DocumentParser


class DocumentLoaderService:
    """文档加载统一入口，只负责识别文件类型并分发到对应解析器。"""

    def __init__(self, parsers: list[DocumentParser]) -> None:
        self._parsers: dict[str, DocumentParser] = {}
        for parser in parsers:
            for file_type in parser.supported_types:
                # 扩展名统一大写保存，避免 txt/TXT/md/MD 这类大小写差异影响分发。
                self._parsers[file_type.upper()] = parser

    def load(self, file: BinaryIO, file_name: str) -> list[Document]:
        file_type = self._detect_file_type(file_name)
        parser = self._parsers.get(file_type)
        if parser is None:
            raise UnsupportedFileTypeError(f"unsupported file type: {file_type}")
        return parser.parse(file, file_name, file_type)

    def _detect_file_type(self, file_name: str) -> str:
        suffix = Path(file_name).suffix.lower().lstrip(".")
        # .markdown 在内部作为独立类型保存，便于和 .md 共用 MarkdownParser。
        if suffix == "markdown":
            return "MARKDOWN"
        if suffix:
            return suffix.upper()
        raise UnsupportedFileTypeError("unsupported file type: unknown")
