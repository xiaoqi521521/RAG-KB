from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, field_validator


class FeedbackRequest(BaseModel):
    """用户对助手回答可提交或取消的反馈字段。"""

    model_config = ConfigDict(extra="forbid")

    feedback: Literal[-1, 1] | None
    comment: str | None = None

    @field_validator("comment")
    @classmethod
    def normalize_comment(cls, value: str | None) -> str | None:
        """去除评论首尾空白，并将空白评论统一为未填写。"""
        if value is None:
            return None
        normalized = value.strip()
        return normalized or None


class FeedbackResponse(BaseModel):
    """回答反馈写入后的公开响应。"""

    model_config = ConfigDict(from_attributes=True)

    message_id: int
    feedback: Literal[-1, 1]
    comment: str | None = None
    created_at: datetime
