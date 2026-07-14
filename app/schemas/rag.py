from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator


class RagQueryRequest(BaseModel):
    """RAG 查询请求体。

    Args:
        question: 用户问题，进入服务层前会去除首尾空白。
        kb_ids: 需要查询的知识库 ID 列表，会去重并保持首次出现顺序。
        session_id: 可选会话 ID；由具体入口决定是否读取或写入会话历史。
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
        reference_index: 回答中 `[参考N]` 对应的 1-based 编号。
        document_id: 来源文档 ID。
        document_name: 来源文档名称。
        kb_id: 来源知识库 ID。
        chunk_id: 来源 chunk ID。
        chunk_index: chunk 在文档中的序号。
        page_number: 来源页码；非分页文档为 None。
        section_title: 来源章节标题；无法识别章节时为 None。
        excerpt: 实际进入 Prompt 的来源内容摘要。
        score: 最终排序分；v4 精排成功时为 Reranker 分数，精排降级时为 RRF 分数。
    """

    reference_index: int
    document_id: int
    document_name: str
    kb_id: int
    chunk_id: int
    chunk_index: int
    page_number: int | None
    section_title: str | None
    excerpt: str
    score: float


class RagQueryResponse(BaseModel):
    """RAG 查询响应体。

    Args:
        answer: 生成答案或固定拒答文案。
        sources: 最终返回给调用方的引用来源列表。
        hit_count: 最终引用来源数量，与 sources 数量一致。
        latency_ms: 本次查询总耗时，单位毫秒。
    """

    answer: str
    sources: list[SourceCitation]
    hit_count: int
    latency_ms: int


class ChatQueryResponse(RagQueryResponse):
    """会话化同步问答响应。

    Args:
        session_id: 本轮使用的会话 ID，前端应在后续追问时原样传回。
    """

    session_id: str


class ChatSessionResponse(BaseModel):
    """面向前端的对话会话记录。"""

    model_config = ConfigDict(from_attributes=True)

    id: str
    user_id: int
    kb_ids: str
    title: str | None
    message_count: int
    created_at: datetime
    last_active_at: datetime
    is_deleted: bool


class ChatMessageResponse(BaseModel):
    """面向前端的对话消息记录。"""

    model_config = ConfigDict(from_attributes=True)

    id: int
    session_id: str
    role: str
    content: str
    sources: dict[str, Any] | list[dict[str, Any]] | None
    token_count: int | None
    latency_ms: int | None
    feedback: int | None
    created_at: datetime
