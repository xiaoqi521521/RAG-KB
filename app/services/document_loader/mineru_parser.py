import tempfile
from pathlib import Path
from typing import Any, BinaryIO, Callable

from langchain_core.documents import Document

from app.core.config import Settings, get_settings
from app.services.document_loader.exceptions import EmptyDocumentError, ExternalParserError
from app.services.document_loader.markdown_parser import MarkdownParser
from app.services.document_loader.parsers import build_metadata

LoaderFactory = Callable[..., Any]


class MinerUDocumentParser:
    """MinerU 官方 LangChain SDK 解析器，负责收敛 SDK 返回的 Document。"""

    @property
    def supported_types(self) -> set[str]:
        return {"PDF", "DOC", "DOCX"}

    @property
    def parser_name(self) -> str:
        return "mineru"

    def __init__(
        self,
        settings: Settings | None = None,
        loader_factory: LoaderFactory | None = None,
    ) -> None:
        self.settings = settings or get_settings()
        self.loader_factory = loader_factory or self._default_loader_factory

    def parse(self, file: BinaryIO, file_name: str, file_type: str) -> list[Document]:
        # 复杂文档不做低质量本地兜底，避免用户误以为已完成高质量解析。
        if not self.settings.mineru_enabled:
            raise ExternalParserError("MinerU is disabled")
        if self.settings.mineru_mode != "sdk":
            raise ExternalParserError(f"unsupported MinerU mode: {self.settings.mineru_mode}")
        sdk_mode = self._sdk_mode()
        if sdk_mode == "precision" and not self.settings.mineru_api_key:
            raise ExternalParserError("MinerU API key is not configured")

        temp_path = self._write_temp_file(file, file_name)
        try:
            loader = self.loader_factory(
                source=str(temp_path),
                mode=sdk_mode,
                token=self.settings.mineru_api_key or None,
                language="ch",
                timeout=self.settings.mineru_timeout_seconds,
                split_pages=file_type == "PDF",
            )
            sdk_docs = loader.load()
        except Exception as exc:
            raise ExternalParserError(f"MinerU SDK request failed: {exc}") from exc
        finally:
            temp_path.unlink(missing_ok=True)

        docs = self._convert_sdk_documents(sdk_docs, file_name, file_type)
        if not docs:
            raise EmptyDocumentError("empty MinerU document")
        return docs

    def _default_loader_factory(self, **kwargs: Any) -> Any:
        """创建默认的 MinerULoader 实例。"""
        from langchain_mineru import MinerULoader

        return MinerULoader(**kwargs)

    def _sdk_mode(self) -> str:
        """根据配置确定 SDK 模式（precision/flash）。"""
        api_type = self.settings.mineru_api_type.lower()
        if api_type in {"accurate", "precision"}:
            return "precision"
        if api_type == "flash":
            return "flash"
        raise ExternalParserError(f"unsupported MinerU API type: {self.settings.mineru_api_type}")

    def _write_temp_file(self, file: BinaryIO, file_name: str) -> Path:
        """将文件流写入临时文件，供 SDK 读取。"""
        suffix = Path(file_name).suffix
        with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as temp_file:
            temp_file.write(file.read())
            return Path(temp_file.name)

    def _convert_sdk_documents(
        self,
        sdk_docs: list[Document],
        file_name: str,
        file_type: str,
    ) -> list[Document]:
        """转换 SDK 返回的 Document 为标准格式，提取元数据并清理 Markdown。"""
        docs: list[Document] = []
        for index, sdk_doc in enumerate(sdk_docs, start=1):
            text = self._first_text(sdk_doc.page_content)
            if not text:
                continue
            metadata = sdk_doc.metadata or {}
            page_num = self._page_num(metadata.get("page"), index)
            section_title = self._first_text(metadata.get("section_title")) or self._detect_title(text)
            docs.append(
                Document(
                    page_content=MarkdownParser()._strip_markdown(text),
                    metadata=build_metadata(
                        file_name=file_name,
                        file_type=file_type,
                        page_num=page_num,
                        parser=self.parser_name,
                        section_title=section_title,
                        title=self._first_text(metadata.get("title")),
                    ),
                )
            )
        return docs

    def _first_text(self, value: Any) -> str | None:
        """只接受非空字符串，避免把复杂对象误写入 metadata 或正文。"""
        if isinstance(value, str):
            text = value.strip()
            return text or None
        return None

    def _page_num(self, value: Any, fallback: int) -> int:
        """安全转换页码，失败时使用备用值。"""
        try:
            return int(value)
        except (TypeError, ValueError):
            return fallback

    def _detect_title(self, text: str) -> str | None:
        """从 Markdown 标题行中提取章节标题，作为 MinerU 缺省标题的补充。"""
        for line in text.splitlines():
            stripped = line.strip()
            if stripped.startswith("#"):
                return stripped.lstrip("#").strip() or None
        return None
