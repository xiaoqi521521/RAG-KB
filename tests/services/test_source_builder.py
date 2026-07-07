from __future__ import annotations

from app.repositories.chunks import ChunkSearchHit
from app.services.source_builder import SourceBuilder


def _hit(
    *,
    chunk_id: int = 10,
    doc_id: int = 1,
    document_name: str = "研发规范.md",
    kb_id: int = 2,
    chunk_index: int = 3,
    content: str = "代码提交前必须通过本地测试。",
    page_num: int | None = None,
    section_title: str | None = "代码提交",
    score: float = 0.82,
) -> ChunkSearchHit:
    return ChunkSearchHit(
        chunk_id=chunk_id,
        doc_id=doc_id,
        document_name=document_name,
        kb_id=kb_id,
        chunk_index=chunk_index,
        content=content,
        page_num=page_num,
        section_title=section_title,
        score=score,
    )


def test_build_context_and_sources_with_reference_numbers() -> None:
    builder = SourceBuilder(max_context_chars=1000)

    context, sources = builder.build([_hit(), _hit(chunk_id=11, chunk_index=4)], return_top_n=2)

    assert "[参考1]" in context
    assert "[参考2]" in context
    assert "文档：研发规范.md" in context
    assert "页码：不适用" in context
    assert sources[0].chunk_id == 10
    assert sources[0].document_id == 1
    assert sources[0].page_number is None
    assert sources[1].chunk_id == 11


def test_build_limits_sources_to_return_top_n() -> None:
    builder = SourceBuilder(max_context_chars=1000)

    context, sources = builder.build(
        [_hit(chunk_id=10), _hit(chunk_id=11), _hit(chunk_id=12)],
        return_top_n=2,
    )

    assert len(sources) == 2
    assert sources[-1].chunk_id == 11
    assert "[参考3]" not in context


def test_build_truncates_context_by_character_budget() -> None:
    builder = SourceBuilder(max_context_chars=120)

    context, sources = builder.build(
        [_hit(content="很长的制度内容" * 30)],
        return_top_n=1,
    )

    assert len(context) <= 120
    assert sources[0].chunk_id == 10
