from langchain_core.documents import Document

from app.services.document_loader.markdown_parser import MarkdownParser
from app.services.document_loader.mineru_client import MinerULoaderClient
from app.services.document_loader.parsers import build_metadata

WORD_MIN_SECTION_CHARS = 200


class WordParser(MinerULoaderClient):
    """Word 解析器，将 MinerU 输出的 Markdown 文本重组为逻辑章节。

    Word 文档没有稳定页码语义，因此这里不按页保留结果，
    而是基于标题和最小章节长度再次切分，生成更适合后续检索的段落。
    """

    split_pages = False

    @property
    def supported_types(self) -> set[str]:
        return {"DOC", "DOCX"}

    def _convert_sdk_documents(
        self,
        sdk_docs: list[Document],
        file_name: str,
        file_type: str,
    ) -> list[Document]:
        """把 MinerU 的 Word 解析结果转换为项目内部 `Document` 列表。

        Args:
            sdk_docs: MinerU 返回的原始文档列表，通常是一整篇或少量大段文本。
            file_name: 原始文件名，用于写入标准 metadata。
            file_type: 当前项目内部识别出的文件类型。

        Returns:
            按逻辑章节切分并清洗后的 LangChain `Document` 列表。
        """
        docs: list[Document] = []
        # Word 文本通常比 Markdown 原文更长，阈值调大，避免把过短标题段切得过碎。
        markdown_parser = MarkdownParser(min_section_chars=WORD_MIN_SECTION_CHARS)

        # 第一步：遍历 MinerU 返回结果，过滤空内容。
        for sdk_doc in sdk_docs:
            text = self._first_text(sdk_doc.page_content)
            if not text:
                continue

            # 第二步：按标题切为逻辑章节，而不是沿用不稳定的“页”概念。
            for section_title, section_text in markdown_parser._split_sections(text):
                cleaned_text = markdown_parser._strip_markdown(section_text)
                # 去掉 Markdown 标记后可能只剩空白，此类章节没有检索价值，直接跳过。
                if not cleaned_text:
                    continue

                # 第三步：为每个逻辑章节生成标准文档对象，页码字段在这里承载顺序号语义。
                docs.append(
                    Document(
                        page_content=cleaned_text,
                        metadata=build_metadata(
                            file_name=file_name,
                            file_type=file_type,
                            page_num=len(docs) + 1,
                            section_title=section_title,
                        ),
                    )
                )
        return docs
