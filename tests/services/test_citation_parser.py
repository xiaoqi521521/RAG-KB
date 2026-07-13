from app.services.citation_parser import CitationParser


def test_extract_indices_supports_grouped_bare_and_spaced_markers() -> None:
    parser = CitationParser()

    indices = parser.extract_indices(
        "年假为 5 天（来源：[参考2][ 参考 1 ]），申请入口见流程说明[参考2]。"
    )

    assert indices == [2, 1]
