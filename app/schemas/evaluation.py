from __future__ import annotations

from datetime import datetime
import re
from typing import Annotated, Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


PositiveId = Annotated[int, Field(gt=0)]
EVAL_VERSION_PATTERN = re.compile(r"(?:v)?([0-9]+)(?:[_-].*)?", re.IGNORECASE)


def normalize_eval_version(value: int | str) -> int:
    """规范化评估版本为正整数，兼容旧的 ``vN_*`` 标识。"""
    if isinstance(value, bool):
        raise ValueError("invalid evaluation version")
    if isinstance(value, int):
        version = value
    elif isinstance(value, str):
        normalized = value.strip()
        match = EVAL_VERSION_PATTERN.fullmatch(normalized)
        if match is None:
            raise ValueError("invalid evaluation version")
        version = int(match.group(1))
    else:
        raise ValueError("invalid evaluation version")
    if version <= 0 or version > 2_147_483_647:
        raise ValueError("invalid evaluation version")
    return version


class EvalDatasetWriteRequest(BaseModel):
    """人工创建或编辑标准问题时允许提交的字段。"""

    model_config = ConfigDict(extra="forbid")

    question: str
    expected_answer: str | None = None
    expected_chunk_ids: list[PositiveId] | None = None
    status: str | None = None

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

    @field_validator("status")
    @classmethod
    def validate_status(cls, value: str | None) -> str | None:
        """校验状态值合法性。"""
        if value is None:
            return None
        if value not in {"CANDIDATE", "ACTIVE", "NEEDS_REVIEW", "ARCHIVED"}:
            raise ValueError("invalid status value")
        return value

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
    updated_at: datetime | None = None


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


class EvaluationReportItem(BaseModel):
    """检索与生成评估版本的聚合报告。"""

    model_config = ConfigDict(from_attributes=True)

    kb_id: int
    eval_version: int
    total_questions: int
    success_count: int
    partial_count: int
    failed_count: int
    retrieval_sample_count: int
    hit_count: int
    hit_rate_at_5: float | None = None
    mrr_at_5: float | None = None
    faithfulness_sample_count: int
    avg_faithfulness: float | None = None
    answer_relevancy_sample_count: int
    avg_answer_relevancy: float | None = None
    context_recall_sample_count: int
    avg_context_recall: float | None = None
    context_precision_sample_count: int
    avg_context_precision: float | None = None
    refusal_count: int
    refusal_rate: float
    eval_at: datetime


class EvaluationHistoryPage(BaseModel):
    """评估历史分页响应及可用版本列表。"""

    items: list[EvaluationReportItem]
    total: int
    page: int
    page_size: int
    versions: list[int]
