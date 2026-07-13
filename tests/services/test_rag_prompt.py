from app.services.rag_prompt import build_v4_system_prompt


def test_v4_prompt_requires_canonical_citations_for_each_fact() -> None:
    prompt = build_v4_system_prompt("[参考1]\n年假为 5 天。", reference_count=1)

    assert "每条事实" in prompt
    assert "（来源：[参考1]）" in prompt
    assert "[参考1]\n年假为 5 天。" in prompt


def test_v4_prompt_requires_complete_ordered_process_steps() -> None:
    prompt = build_v4_system_prompt("[参考1]\n1. 提交申请\n2. 主管审批", reference_count=1)

    assert "按原文顺序逐条完整列出" in prompt
    assert "不得用一句空泛概括替代" in prompt


def test_v4_prompt_supports_multiple_sources_and_only_visible_content() -> None:
    prompt = build_v4_system_prompt("[参考1]\n制度片段", reference_count=2)

    assert "（来源：[参考1][参考2]）" in prompt
    assert "只回答实际可见内容" in prompt
    assert "通常情况下" in prompt
