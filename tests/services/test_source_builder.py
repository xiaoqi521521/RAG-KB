from __future__ import annotations

from app.repositories.chunks import ChunkSearchHit
from app.services.source_builder import CitationSelectionStatus, SourceBuilder


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
    assert sources[0].reference_index == 1
    assert sources[0].excerpt == "代码提交前必须通过本地测试。"
    assert sources[0].page_number is None
    assert sources[1].chunk_id == 11
    assert sources[1].reference_index == 2


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
    assert sources[0].excerpt in context
    assert len(sources[0].excerpt) < len("很长的制度内容" * 30)


def test_resolve_citations_returns_valid_sources_in_answer_order() -> None:
    builder = SourceBuilder(max_context_chars=1000)
    _, available_sources = builder.build(
        [_hit(chunk_id=10), _hit(chunk_id=11), _hit(chunk_id=12)],
        return_top_n=3,
    )

    result = builder.resolve_citations(
        "先执行检查（来源：[参考2][参考1]）。",
        available_sources,
    )

    assert result.status is CitationSelectionStatus.EXACT
    assert [source.reference_index for source in result.sources] == [2, 1]
    assert [source.chunk_id for source in result.sources] == [11, 10]


def test_resolve_citations_returns_all_sources_when_answer_has_no_markers() -> None:
    builder = SourceBuilder(max_context_chars=1000)
    _, available_sources = builder.build(
        [_hit(chunk_id=10), _hit(chunk_id=11)],
        return_top_n=2,
    )

    result = builder.resolve_citations("需要先完成审批流程。", available_sources)

    assert result.status is CitationSelectionStatus.FALLBACK_ALL
    assert result.sources == available_sources
    assert result.referenced_count == 0
    assert result.valid_count == 0
    assert result.invalid_count == 0


def test_resolve_citations_ignores_invalid_indices_when_valid_sources_exist() -> None:
    builder = SourceBuilder(max_context_chars=1000)
    _, available_sources = builder.build(
        [_hit(chunk_id=10), _hit(chunk_id=11)],
        return_top_n=2,
    )

    result = builder.resolve_citations(
        "第二条有效（来源：[参考2][参考999]）。",
        available_sources,
    )

    assert result.status is CitationSelectionStatus.EXACT
    assert [source.reference_index for source in result.sources] == [2]
    assert result.referenced_count == 2
    assert result.valid_count == 1
    assert result.invalid_count == 1


def test_resolve_citations_returns_empty_when_all_indices_are_invalid() -> None:
    builder = SourceBuilder(max_context_chars=1000)
    _, available_sources = builder.build([_hit(chunk_id=10)], return_top_n=1)

    result = builder.resolve_citations("不存在的来源[参考0][参考999]。", available_sources)

    assert result.status is CitationSelectionStatus.INVALID
    assert result.sources == []
    assert result.referenced_count == 2
    assert result.valid_count == 0
    assert result.invalid_count == 2


def test_resolve_citations_treats_oversized_reference_number_as_invalid() -> None:
    builder = SourceBuilder(max_context_chars=1000)
    _, available_sources = builder.build([_hit(chunk_id=10)], return_top_n=1)
    oversized_index = "9" * 5000

    result = builder.resolve_citations(
        f"引用编号异常[参考{oversized_index}]。",
        available_sources,
    )

    assert result.status is CitationSelectionStatus.INVALID
    assert result.sources == []
    assert result.referenced_count == 1
    assert result.valid_count == 0
    assert result.invalid_count == 1


def test_build_limits_source_excerpt_to_200_characters() -> None:
    builder = SourceBuilder(max_context_chars=1000)

    _, sources = builder.build([_hit(content="制" * 250)], return_top_n=1)

    assert sources[0].excerpt == "制" * 200
