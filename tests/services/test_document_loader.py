from io import BytesIO
from pathlib import Path

import pytest
import logging
from langchain_core.documents import Document


def _print_docs(case_name: str, docs: list[Document]) -> None:
    print(f"\n[{case_name}] docs_count={len(docs)}")
    for index, doc in enumerate(docs, start=1):
        preview = doc.page_content.replace("\n", "\\n")[:120]
        print(f"  doc#{index} metadata={doc.metadata}")
        print(f"  doc#{index} content_preview={preview}")


def _extract_text(docs: list[Document]) -> str:
    return "\n".join(doc.page_content for doc in docs)


def _build_loader_service(fake_mineru_loader: type):
    from app.core.config import Settings
    from app.services.document_loader import (
        DocumentLoaderService,
        MarkdownParser,
        TxtParser,
    )
    from app.services.document_loader.pdf_parser import PdfParser
    from app.services.document_loader.word_parser import WordParser

    settings = Settings(
        _env_file=None,
        secret_key="test-secret",
        database_url="postgresql+asyncpg://ragkb:ragkb123@localhost:5432/ragkb",
        sync_database_url="postgresql+psycopg://ragkb:ragkb123@localhost:5432/ragkb",
        reranker_endpoint="https://example.test/rerank",
        mineru_api_key="token",
    )
    return DocumentLoaderService(
        [
            TxtParser(),
            MarkdownParser(),
            PdfParser(settings=settings, loader_factory=fake_mineru_loader),
            WordParser(settings=settings, loader_factory=fake_mineru_loader),
        ]
    )


class FakeMinerULoader:
    calls: list[dict[str, object]] = []

    def __init__(self, **kwargs: object) -> None:
        self.calls.append(kwargs)

    def load(self) -> list[Document]:
        source = str(self.calls[-1]["source"])
        suffix = Path(source).suffix.lower()

        if suffix == ".pdf":
            return [
                Document(
                    page_content="# 总则\n员工手册正文",
                    metadata={"page": 1, "filename": Path(source).name, "title": "员工手册"},
                ),
                Document(
                    page_content="## 考勤\n考勤制度正文",
                    metadata={"page": 2, "filename": Path(source).name},
                ),
            ]

        if suffix == ".docx":
            long_text = "Word 正文。" * 30
            return [
                Document(
                    page_content=f"# 制度\n{long_text}\n\n## 附则\n短内容",
                    metadata={"filename": Path(source).name},
                )
            ]

        raise ValueError(f"unsupported fake MinerU source: {source}")


def test_parse_txt_file():
    """对齐参考文件 parseTxtFile：通过统一入口解析 TXT。"""
    service = _build_loader_service(FakeMinerULoader)

    docs = service.load(BytesIO("员工手册\r\n考勤制度".encode()), "hr-handbook.txt")
    text = _extract_text(docs)
    _print_docs("TXT 解析结果", docs)

    assert docs
    assert text
    assert "员工手册" in text
    assert docs[0].metadata == {
        "source": "hr-handbook.txt",
        "file_type": "TXT",
        "page_num": 1,
    }


def test_parse_pdf():
    """对齐参考文件 parsePdf：通过统一入口解析 PDF，并保留页级结果。"""
    FakeMinerULoader.calls = []
    service = _build_loader_service(FakeMinerULoader)

    docs = service.load(BytesIO(b"pdf bytes"), "policy.pdf")
    text = _extract_text(docs)
    _print_docs("PDF 解析结果", docs)

    assert docs
    assert text
    assert len(docs) > 0
    assert FakeMinerULoader.calls[-1]["split_pages"] is True
    assert docs[0].metadata["source"] == "policy.pdf"
    assert docs[0].metadata["file_type"] == "PDF"
    assert docs[0].metadata["page_num"] == 1
    assert all("title" not in doc.metadata for doc in docs)


def test_parse_pdf_logs_mineru_request_summary(caplog):
    """MinerU 请求只保留开始和结束摘要，不依赖 httpx 明细日志。"""
    FakeMinerULoader.calls = []
    service = _build_loader_service(FakeMinerULoader)

    with caplog.at_level(logging.INFO, logger="app.services.document_loader.mineru_client"):
        service.load(BytesIO(b"pdf bytes"), "policy.pdf")

    messages = [record.getMessage() for record in caplog.records]
    assert "MinerU解析开始了..." in messages
    assert "MinerU解析结束了..." in messages


def test_parse_docx():
    """对齐参考文件 parseDocx：通过统一入口解析 Word，并返回逻辑分节。"""
    FakeMinerULoader.calls = []
    service = _build_loader_service(FakeMinerULoader)

    docs = service.load(BytesIO(b"docx bytes"), "policy.docx")
    text = _extract_text(docs)
    _print_docs("DOCX 解析结果", docs)

    assert docs
    assert text
    assert len(docs) > 0
    assert FakeMinerULoader.calls[-1]["split_pages"] is False
    assert docs[0].metadata["source"] == "policy.docx"
    assert docs[0].metadata["file_type"] == "DOCX"
    assert docs[0].metadata["page_num"] == 1


def test_parse_md():
    """对齐参考文件 parseMd：通过统一入口解析 Markdown。"""
    service = _build_loader_service(FakeMinerULoader)
    long_install_text = "安装步骤说明。" * 20
    markdown = f"""# 安装
{long_install_text}

```
# 这不是标题
print("hello")
```

## 配置
配置说明，[官网](https://example.test)。
"""

    docs = service.load(BytesIO(markdown.encode()), "hr-handbook.md")
    text = _extract_text(docs)
    _print_docs("Markdown 解析结果", docs)

    assert docs
    assert text
    assert "安装" in text
    assert "官网" in text
    assert "这不是标题" not in [doc.metadata.get("section_title") for doc in docs]
    assert all(doc.metadata["source"] == "hr-handbook.md" for doc in docs)
    assert all(doc.metadata["file_type"] == "MD" for doc in docs)
    assert all("title" not in doc.metadata for doc in docs)


def test_unsupported_type_raises_error():
    """对齐参考文件 unsupportedTypeReturnsFailure：不支持的类型给出明确失败。"""
    from app.services.document_loader import UnsupportedFileTypeError

    service = _build_loader_service(FakeMinerULoader)

    with pytest.raises(UnsupportedFileTypeError, match="unsupported file type"):
        service.load(BytesIO(b"zip"), "test.xyz")
