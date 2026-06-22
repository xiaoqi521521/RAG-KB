from io import BytesIO
from pathlib import Path

import pytest
from langchain_core.documents import Document


def test_settings_loads_mineru_defaults():
    from app.core.config import Settings

    settings = Settings(
        _env_file=None,
        secret_key="test-secret",
        database_url="postgresql+asyncpg://ragkb:ragkb123@localhost:5432/ragkb",
        sync_database_url="postgresql+psycopg://ragkb:ragkb123@localhost:5432/ragkb",
        reranker_endpoint="https://example.test/rerank",
    )

    assert settings.mineru_enabled is True
    assert settings.mineru_mode == "sdk"
    assert settings.mineru_api_type == "precision"
    assert settings.mineru_api_url == ""
    assert settings.mineru_api_key == ""
    assert settings.mineru_timeout_seconds == 300


def test_txt_parser_returns_document_with_required_metadata():
    from app.services.document_loader import TxtParser

    parser = TxtParser()

    docs = parser.parse(BytesIO("员工手册\r\n考勤制度".encode()), "hr-handbook.txt", "TXT")

    assert len(docs) == 1
    assert docs[0].page_content == "员工手册\n考勤制度"
    assert docs[0].metadata == {
        "source": "hr-handbook.txt",
        "file_type": "TXT",
        "page_num": 1,
        "parser": "txt",
    }


def test_txt_parser_rejects_empty_text():
    from app.services.document_loader import EmptyDocumentError, TxtParser

    parser = TxtParser()

    with pytest.raises(EmptyDocumentError, match="empty"):
        parser.parse(BytesIO(b"  \r\n\t  "), "empty.txt", "TXT")


def test_markdown_parser_splits_headings_and_ignores_code_block_hashes():
    from app.services.document_loader import MarkdownParser

    markdown = """# 安装
安装步骤说明。

```python
# 这不是标题
print("hello")
```

## 配置
配置说明，[官网](https://example.test)。
"""
    parser = MarkdownParser()

    docs = parser.parse(BytesIO(markdown.encode()), "guide.md", "MD")

    assert [doc.metadata["section_title"] for doc in docs] == ["安装", "配置"]
    assert [doc.metadata["page_num"] for doc in docs] == [1, 2]
    assert all(doc.metadata["source"] == "guide.md" for doc in docs)
    assert all(doc.metadata["file_type"] == "MD" for doc in docs)
    assert all(doc.metadata["parser"] == "markdown" for doc in docs)
    assert "[代码块]" in docs[0].page_content
    assert "这不是标题" not in [doc.metadata["section_title"] for doc in docs]
    assert "官网" in docs[1].page_content


def test_document_loader_service_dispatches_by_file_type():
    from app.services.document_loader import DocumentLoaderService, TxtParser

    service = DocumentLoaderService([TxtParser()])

    docs = service.load(BytesIO("hello".encode()), "note.txt")

    assert docs[0].page_content == "hello"
    assert docs[0].metadata["file_type"] == "TXT"


def test_document_loader_service_rejects_unsupported_file_type():
    from app.services.document_loader import DocumentLoaderService, UnsupportedFileTypeError

    service = DocumentLoaderService([])

    with pytest.raises(UnsupportedFileTypeError, match="unsupported file type"):
        service.load(BytesIO(b"zip"), "archive.zip")


def test_mineru_parser_uses_official_loader_for_pdf_pages():
    from app.core.config import Settings
    from app.services.document_loader import MinerUDocumentParser

    calls: list[dict[str, object]] = []

    class FakeMinerULoader:
        def __init__(self, **kwargs: object) -> None:
            calls.append(kwargs)

        def load(self) -> list[Document]:
            source = str(calls[-1]["source"])
            assert Path(source).suffix == ".pdf"
            assert calls[-1]["mode"] == "precision"
            assert calls[-1]["token"] == "token"
            assert calls[-1]["split_pages"] is True
            return [
                Document(
                    page_content="# 总则\n员工手册正文",
                    metadata={"page": 1, "filename": Path(source).name},
                ),
                Document(
                    page_content="## 考勤\n考勤制度正文",
                    metadata={"page": 2, "filename": Path(source).name},
                ),
            ]

    settings = Settings(
        _env_file=None,
        secret_key="test-secret",
        database_url="postgresql+asyncpg://ragkb:ragkb123@localhost:5432/ragkb",
        sync_database_url="postgresql+psycopg://ragkb:ragkb123@localhost:5432/ragkb",
        reranker_endpoint="https://example.test/rerank",
        mineru_api_key="token",
    )
    parser = MinerUDocumentParser(settings=settings, loader_factory=FakeMinerULoader)

    docs = parser.parse(BytesIO(b"pdf bytes"), "policy.pdf", "PDF")

    assert [doc.page_content for doc in docs] == ["总则\n员工手册正文", "考勤\n考勤制度正文"]
    assert docs[0].metadata == {
        "source": "policy.pdf",
        "file_type": "PDF",
        "page_num": 1,
        "section_title": "总则",
        "parser": "mineru",
    }
    assert docs[1].metadata["section_title"] == "考勤"


def test_mineru_parser_uses_official_loader_for_word_as_single_document():
    from app.core.config import Settings
    from app.services.document_loader import MinerUDocumentParser

    calls: list[dict[str, object]] = []

    class FakeMinerULoader:
        def __init__(self, **kwargs: object) -> None:
            calls.append(kwargs)

        def load(self) -> list[Document]:
            source = str(calls[-1]["source"])
            assert Path(source).suffix == ".docx"
            assert calls[-1]["split_pages"] is False
            return [
                Document(
                    page_content="# 制度\nWord 正文",
                    metadata={"filename": Path(source).name},
                )
            ]

    settings = Settings(
        _env_file=None,
        secret_key="test-secret",
        database_url="postgresql+asyncpg://ragkb:ragkb123@localhost:5432/ragkb",
        sync_database_url="postgresql+psycopg://ragkb:ragkb123@localhost:5432/ragkb",
        reranker_endpoint="https://example.test/rerank",
        mineru_api_key="token",
    )
    parser = MinerUDocumentParser(settings=settings, loader_factory=FakeMinerULoader)

    docs = parser.parse(BytesIO(b"docx bytes"), "policy.docx", "DOCX")

    assert len(docs) == 1
    assert docs[0].page_content == "制度\nWord 正文"
    assert docs[0].metadata == {
        "source": "policy.docx",
        "file_type": "DOCX",
        "page_num": 1,
        "section_title": "制度",
        "parser": "mineru",
    }


def test_mineru_parser_raises_external_error_for_sdk_failure():
    from app.core.config import Settings
    from app.services.document_loader import ExternalParserError, MinerUDocumentParser

    class FailingMinerULoader:
        def __init__(self, **kwargs: object) -> None:
            pass

        def load(self) -> list[Document]:
            raise ValueError("sdk failed")

    settings = Settings(
        _env_file=None,
        secret_key="test-secret",
        database_url="postgresql+asyncpg://ragkb:ragkb123@localhost:5432/ragkb",
        sync_database_url="postgresql+psycopg://ragkb:ragkb123@localhost:5432/ragkb",
        reranker_endpoint="https://example.test/rerank",
        mineru_api_key="token",
    )
    parser = MinerUDocumentParser(settings=settings, loader_factory=FailingMinerULoader)

    with pytest.raises(ExternalParserError, match="MinerU"):
        parser.parse(BytesIO(b"pdf bytes"), "policy.pdf", "PDF")
