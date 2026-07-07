from __future__ import annotations

from pydantic import BaseModel, Field, field_validator


class RagQueryRequest(BaseModel):
    """RAG 查询请求体。

    Args:
        question: 用户问题，进入服务层前会去除首尾空白。
        kb_ids: 需要查询的知识库 ID 列表，会去重并保持首次出现顺序。
        session_id: 可选会话 ID；当前阶段不读取或写入会话历史。
    """

    question: str = Field(min_length=1, max_length=2000)
    kb_ids: list[int] = Field(min_length=1, max_length=20)
    session_id: str | None = None

    @field_validator("question")
    @classmethod
    def normalize_question(cls, value: str) -> str:
        """清理问题首尾空白，返回可直接用于检索的文本。"""
        normalized = value.strip()
        if not normalized:
            raise ValueError("question must not be blank")
        return normalized

    @field_validator("kb_ids")
    @classmethod
    def normalize_kb_ids(cls, value: list[int]) -> list[int]:
        """去重知识库 ID 并拒绝非正数，返回保持顺序的 ID 列表。"""
        normalized: list[int] = []
        seen: set[int] = set()
        for kb_id in value:
            if kb_id <= 0:
                raise ValueError("kb_ids must be positive integers")
            if kb_id in seen:
                continue
            seen.add(kb_id)
            normalized.append(kb_id)
        if not normalized:
            raise ValueError("kb_ids must not be empty")
        return normalized


class SourceCitation(BaseModel):
    """回答引用来源，供前端展示和后续溯源。

    Args:
        document_id: 来源文档 ID。
        document_name: 来源文档名称。
        kb_id: 来源知识库 ID。
        chunk_id: 来源 chunk ID。
        chunk_index: chunk 在文档中的序号。
        page_number: 来源页码；非分页文档为 None。
        section_title: 来源章节标题；无法识别章节时为 None。
        score: 最终检索排序分；混合检索阶段为 RRF 分数，不是相似度百分比。
    """

    document_id: int
    document_name: str
    kb_id: int
    chunk_id: int
    chunk_index: int
    page_number: int | None
    section_title: str | None
    score: float


class RagQueryResponse(BaseModel):
    """RAG 查询响应体。

    Args:
        answer: 生成答案或固定拒答文案。
        sources: 实际进入 Prompt 的引用来源列表。
        hit_count: 实际进入 Prompt 的引用 chunk 数量，与 sources 数量一致。
        latency_ms: 本次查询总耗时，单位毫秒。
    """

    answer: str
    sources: list[SourceCitation]
    hit_count: int
    latency_ms: int
