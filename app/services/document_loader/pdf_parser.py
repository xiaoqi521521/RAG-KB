from typing import Any

from langchain_core.documents import Document

from app.services.document_loader.markdown_parser import MarkdownParser
from app.services.document_loader.mineru_client import MinerULoaderClient
from app.services.document_loader.parsers import build_metadata


class PdfParser(MinerULoaderClient):
    """PDF 解析器，基于 MinerU 页级结果构建内部文档对象。

    PDF 天然有稳定页码，因此这里保持 `split_pages=True`，
    优先保留外部解析返回的页级边界和页码信息。
    """

    split_pages = True

    @property
    def supported_types(self) -> set[str]:
        return {"PDF"}

    def _convert_sdk_documents(
        self,
        sdk_docs: list[Document],
        file_name: str,
        file_type: str,
    ) -> list[Document]:
        """把 MinerU 的 PDF 解析结果转换为项目内部 `Document` 列表。

        Args:
            sdk_docs: MinerU 返回的原始文档列表，通常一项对应一页。
            file_name: 原始文件名，用于写入标准 metadata。
            file_type: 当前项目内部识别出的文件类型。

        Returns:
            清洗并补齐 metadata 后的 LangChain `Document` 列表。
        """
        docs: list[Document] = []
        markdown_parser = MarkdownParser()

        # 第一步：逐页读取 MinerU 输出，过滤空文本页。
        for index, sdk_doc in enumerate(sdk_docs, start=1):
            text = self._first_text(sdk_doc.page_content)
            if not text:
                continue

            # 第二步：优先使用 SDK 页码和章节标题；缺失时退回本地推断结果。
            metadata = sdk_doc.metadata or {}
            page_num = self._page_num(metadata.get("page"), index)
            section_title = self._first_text(metadata.get("section_title")) or self._detect_section_title(text)

            # 第三步：去掉 Markdown 标记，组装为项目统一的文档结构。
            docs.append(
                Document(
                    page_content=markdown_parser._strip_markdown(text),
                    metadata=build_metadata(
                        file_name=file_name,
                        file_type=file_type,
                        page_num=page_num,
                        section_title=section_title,
                    ),
                )
            )
        return docs

    def _page_num(self, value: Any, fallback: int) -> int:
        """安全转换页码字段，异常时退回当前结果下标。

        MinerU 返回的 `page` 可能缺失、为字符串，甚至是异常值。
        这里统一转成 `int`，失败时至少保证页码连续可用。
        """
        try:
            return int(value)
        except (TypeError, ValueError):
            return fallback

    def _detect_section_title(self, text: str) -> str | None:
        """从 MinerU 返回的 Markdown 文本中提取兜底章节标题。

        当 SDK metadata 没有显式 `section_title` 时，尝试取首个 Markdown 标题行，
        提高后续引用溯源时的可读性。
        """
        for line in text.splitlines():
            stripped = line.strip()
            # 只要遇到首个标题行就返回，避免把正文中的后续标题误当成当前页主标题。
            if stripped.startswith("#"):
                return stripped.lstrip("#").strip() or None
        return None
