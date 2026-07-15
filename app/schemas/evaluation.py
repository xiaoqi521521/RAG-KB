from __future__ import annotations

from datetime import datetime
from typing import Annotated, Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


PositiveId = Annotated[int, Field(gt=0)]


class EvalDatasetWriteRequest(BaseModel):
    """人工创建或编辑标准问题时允许提交的字段。"""

    model_config = ConfigDict(extra="forbid")

    question: str
    expected_answer: str | None = None
    expected_chunk_ids: list[PositiveId] | None = None

    @field_validator("question")
    @classmethod
    def normalize_question(cls, value: str) -> str:
        """去除问题首尾空白并拒绝空问题。"""
        normalized = value.strip()
        if not normalized:
            raise ValueError("question must not be blank")
        return normalized

    @field_validator("expected_answer")
    @classmethod
    def normalize_expected_answer(cls, value: str | None) -> str | None:
        """将空白期望答案统一为未标注。"""
        if value is None:
            return None
        normalized = value.strip()
        return normalized or None

    @field_validator("expected_chunk_ids")
    @classmethod
    def normalize_chunk_ids(cls, value: list[int] | None) -> list[int] | None:
        """按提交顺序去重，并将空数组统一为未标注。"""
        if not value:
            return None
        return list(dict.fromkeys(value))

    @model_validator(mode="after")
    def require_ground_truth(self) -> Self:
        """人工标准问题至少需要期望答案或期望 chunk。"""
        if self.expected_answer is None and self.expected_chunk_ids is None:
            raise ValueError("expected answer or expected chunk ids is required")
        return self


class EvalDatasetItem(BaseModel):
    """标准问题集列表和写入响应项。"""

    model_config = ConfigDict(from_attributes=True)

    id: int
    kb_id: int
    question: str
    expected_answer: str | None = None
    expected_chunk_ids: list[int] | None = None
    status: str
    review_reason: str | None = None
    source_feedback_id: int | None = None
    created_by: int
    created_at: datetime


class CurrentChunkSummaryItem(BaseModel):
    """当前已发布 chunk 的标注摘要。"""

    model_config = ConfigDict(from_attributes=True)

    chunk_id: int
    document_id: int
    document_name: str
    chunk_index: int
    page_number: int | None = None
    section_title: str | None = None
    token_count: int
    excerpt: str
