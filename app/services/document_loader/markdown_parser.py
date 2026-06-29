import re
from typing import BinaryIO

from langchain_core.documents import Document

from app.services.document_loader.exceptions import EmptyDocumentError
from app.services.document_loader.parsers import build_metadata

HEADING_PATTERN = re.compile(r"^(#{1,3})\s+(.+?)\s*$")
DEFAULT_MIN_SECTION_CHARS = 100


class MarkdownParser:
    """轻量 Markdown 解析器，按标题切分为可追溯的逻辑章节。"""

    def __init__(self, min_section_chars: int = DEFAULT_MIN_SECTION_CHARS) -> None:
        self.min_section_chars = min_section_chars

    @property
    def supported_types(self) -> set[str]:
        return {"MD", "MARKDOWN"}

    def parse(self, file: BinaryIO, file_name: str, file_type: str) -> list[Document]:
        markdown = file.read().decode("utf-8", errors="replace").replace("\r\n", "\n")
        sections = self._split_sections(markdown)

        docs: list[Document] = []
        for index, (section_title, section_text) in enumerate(sections, start=1):
            text = self._strip_markdown(section_text)
            if not text:
                continue
            docs.append(
                Document(
                    page_content=text,
                    metadata=build_metadata(
                        file_name=file_name,
                        file_type=file_type,
                        page_num=index,
                        section_title=section_title,
                    ),
                )
            )

        if not docs:
            raise EmptyDocumentError("empty document")
        return docs

    def _split_sections(self, markdown: str) -> list[tuple[str | None, str]]:
        """按一级/二级标题切分；代码块中的 # 不参与标题判断。"""
        sections: list[tuple[str | None, str]] = []
        current_title: str | None = None
        current_lines: list[str] = []
        in_code_block = False

        for line in markdown.split("\n"):
            if line.startswith("```"):
                # 代码块边界只切换状态，代码块内容保持在当前章节。
                in_code_block = not in_code_block
                current_lines.append(line)
                continue

            match = HEADING_PATTERN.match(line)
            is_section_heading = (
                not in_code_block
                and match is not None
                and match.group(1) in {"#", "##"}
            )
            if is_section_heading:
                if self._section_length(current_lines) > self.min_section_chars:
                    # 遇到新标题时，先收束上一个章节。
                    sections.append((current_title, "\n".join(current_lines)))
                    current_lines = []
                current_title = match.group(2).strip()

            current_lines.append(line)

        if current_lines:
            sections.append((current_title, "\n".join(current_lines)))
        if not sections and markdown.strip():
            sections.append((None, markdown))
        return sections

    def _section_length(self, lines: list[str]) -> int:
        return len("\n".join(lines).strip())

    def _strip_markdown(self, markdown: str) -> str:
        """去掉基础 Markdown 标记，保留对 RAG 有意义的可见文本。"""
        text = re.sub(r"```[\s\S]*?```", " [代码块] ", markdown)
        text = re.sub(r"`([^`]+)`", r"\1", text)
        text = re.sub(r"!\[.*?\]\(.*?\)", " [图片] ", text)
        text = re.sub(r"\[([^\]]+)\]\(.*?\)", r"\1", text)
        text = re.sub(r"(?m)^#{1,6}\s+", "", text)
        text = re.sub(r"\*\*([^*]+)\*\*", r"\1", text)
        text = re.sub(r"\*([^*]+)\*", r"\1", text)
        text = re.sub(r"(?m)^[-*+]\s+", "", text)
        text = re.sub(r"(?m)^\d+\.\s+", "", text)
        text = re.sub(r"\n{3,}", "\n\n", text)
        return text.strip()
