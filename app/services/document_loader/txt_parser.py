from typing import BinaryIO

from langchain_core.documents import Document

from app.services.document_loader.exceptions import DocumentParseError, EmptyDocumentError
from app.services.document_loader.parsers import build_metadata


class TxtParser:
    """轻量 TXT 解析器，直接产出单个 LangChain Document。"""

    @property
    def supported_types(self) -> set[str]:
        return {"TXT"}

    @property
    def parser_name(self) -> str:
        return "txt"

    def parse(self, file: BinaryIO, file_name: str, file_type: str) -> list[Document]:
        raw = file.read()
        text = self._decode(raw)
        # 文本加载层只做格式归一化，不做分块；分块交给后续 chunking 层处理。
        text = text.replace("\ufeff", "").replace("\r\n", "\n").replace("\r", "\n").strip()

        if not text:
            raise EmptyDocumentError("empty document")
        # 控制字符比例过高通常意味着用户误传了二进制文件。
        if self._control_char_ratio(text) > 0.05:
            raise DocumentParseError("text contains too many control characters")

        return [
            Document(
                page_content=text,
                metadata=build_metadata(
                    file_name=file_name,
                    file_type=file_type,
                    page_num=1,
                    parser=self.parser_name,
                ),
            )
        ]

    def _decode(self, raw: bytes) -> str:
        # 先按 UTF-8 读取；替换字符过多时再尝试常见中文 Windows 文档编码 GBK。
        text = raw.decode("utf-8", errors="replace")
        if text.count("\ufffd") / max(len(text), 1) <= 0.02:
            return text
        return raw.decode("gbk", errors="replace")

    def _control_char_ratio(self, text: str) -> float:
        # 换行和制表符属于正常文本控制字符，不计入异常比例。
        control_count = sum(1 for char in text if ord(char) < 32 and char not in "\n\t")
        return control_count / max(len(text), 1)
