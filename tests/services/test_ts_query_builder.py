from __future__ import annotations

from app.services.ts_query_builder import TsQueryBuilder


def test_build_keeps_precise_terms_for_fulltext_search() -> None:
    builder = TsQueryBuilder()

    query_text = builder.build("Commit Message format requirement? SKU-8821 API_KEY 3.2")

    assert query_text is not None
    assert "Commit" in query_text
    assert "Message" in query_text
    assert "SKU-8821" in query_text
    assert "API_KEY" in query_text
    assert "3.2" in query_text
    assert "?" not in query_text


def test_build_returns_none_when_question_has_no_useful_terms() -> None:
    builder = TsQueryBuilder()

    assert builder.build("   what how ?!  ") is None


def test_build_joins_keywords_for_to_tsquery() -> None:
    builder = TsQueryBuilder()

    query_text = builder.build("a, OK; RRF/fusion: id")

    assert query_text == "OK & RRF & fusion & id"
