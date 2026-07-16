from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from app.schemas.rag import SourceCitation


class QueryCacheEntry(BaseModel):
    """查询结果缓存的可校验值，不保存本次请求的会话和耗时信息。"""

    model_config = ConfigDict(extra="forbid")

    version: Literal[1]
    answer: str
    sources: list[SourceCitation]
    hit_count: int = Field(ge=0)
