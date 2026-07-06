import re
from html.parser import HTMLParser
from typing import BinaryIO

from langchain_core.documents import Document

from app.services.document_loader.exceptions import EmptyDocumentError
from app.services.document_loader.parsers import build_metadata

HEADING_PATTERN = re.compile(r"^(#{1,3})\s+(.+?)\s*$")
DEFAULT_MIN_SECTION_CHARS = 100
HTML_TABLE_PATTERN = re.compile(r"<table\b[^>]*>[\s\S]*?</table>", re.IGNORECASE)


class HtmlTablePlainTextParser(HTMLParser):
    """将 MinerU 输出的 HTML 表格转换为适合检索的纯文本行。

    Returns:
        `to_text()` 返回 `[表格]` 标记和 ` | ` 分隔的行文本；空表格返回空字符串。
    """

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.rows: list[list[str]] = []
        self._current_row: list[str] | None = None
        self._current_cell: list[str] | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        """识别表格行和单元格开始标签，返回值为空。"""
        if tag.lower() == "tr":
            self._current_row = []
        elif tag.lower() in {"td", "th"}:
            self._current_cell = []
        elif tag.lower() == "br" and self._current_cell is not None:
            self._current_cell.append(" ")

    def handle_data(self, data: str) -> None:
        """收集单元格内的可见文本，返回值为空。"""
        if self._current_cell is not None:
            self._current_cell.append(data)

    def handle_endtag(self, tag: str) -> None:
        """在单元格或行结束时收束内容，返回值为空。"""
        tag_name = tag.lower()
        if tag_name in {"td", "th"} and self._current_cell is not None:
            cell_text = _normalize_table_cell("".join(self._current_cell))
            if self._current_row is not None:
                self._current_row.append(cell_text)
            self._current_cell = None
        elif tag_name == "tr" and self._current_row is not None:
            if any(cell for cell in self._current_row):
                self.rows.append(self._current_row)
            self._current_row = None

    def to_text(self) -> str:
        """返回 `[表格]` 加管道分隔行，供 Markdown 清洗流程继续处理。"""
        if not self.rows:
            return ""
        lines = ["[表格]"]
        lines.extend(" | ".join(row).strip() for row in self.rows)
        return "\n".join(line for line in lines if line)


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
        text = _convert_html_tables(markdown)
        text = re.sub(r"```[\s\S]*?```", " [代码块] ", text)
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


def _convert_html_tables(markdown: str) -> str:
    """把 Markdown 中的 HTML 表格块转换为 `[表格]` 纯文本表示。"""

    def replace_table(match: re.Match[str]) -> str:
        parser = HtmlTablePlainTextParser()
        parser.feed(match.group(0))
        parser.close()
        table_text = parser.to_text()
        # 解析失败或空表格时保留原文本，避免静默丢失业务内容。
        return f"\n{table_text}\n" if table_text else match.group(0)

    return HTML_TABLE_PATTERN.sub(replace_table, markdown)


def _normalize_table_cell(text: str) -> str:
    """收敛表格单元格空白，返回适合行内展示和 embedding 的文本。"""
    return re.sub(r"\s+", " ", text).strip()
