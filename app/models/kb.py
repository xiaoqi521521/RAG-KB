from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any

from pgvector.sqlalchemy import Vector as PGVector  # type: ignore[import-untyped]
from sqlalchemy import BigInteger, Boolean, DateTime, Float, Integer, SmallInteger, String, Text, text
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.types import UserDefinedType

from app.core.database import Base
from app.core.time import shanghai_now_naive


SHANGHAI_NOW_SQL = text("timezone('Asia/Shanghai', now())")


class TSVector(UserDefinedType):
    cache_ok = True

    def get_col_spec(self, **kw: Any) -> str:
        return "TSVECTOR"


class PermissionSubjectType(StrEnum):
    DEPARTMENT = "DEPARTMENT"
    USER = "USER"


class KbPermissionLevel(StrEnum):
    READ = "READ"
    WRITE = "WRITE"
    ADMIN = "ADMIN"


class DocumentStatus(StrEnum):
    PENDING = "PENDING"
    PROCESSING = "PROCESSING"
    DONE = "DONE"
    FAILED = "FAILED"


class IndexTaskType(StrEnum):
    INDEX = "INDEX"
    REINDEX = "REINDEX"


class IndexTaskStatus(StrEnum):
    PENDING = "PENDING"
    RUNNING = "RUNNING"
    DONE = "DONE"
    FAILED = "FAILED"


class ChatMessageRole(StrEnum):
    USER = "USER"
    ASSISTANT = "ASSISTANT"


class KnowledgeBase(Base):
    __tablename__ = "kb_knowledge_base"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    department_id: Mapped[str] = mapped_column(String(50), nullable=False)
    is_public: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default="false")
    created_by: Mapped[int] = mapped_column(BigInteger, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=shanghai_now_naive, server_default=SHANGHAI_NOW_SQL
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime,
        nullable=False,
        default=shanghai_now_naive,
        server_default=SHANGHAI_NOW_SQL,
        onupdate=shanghai_now_naive,
    )
    is_deleted: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false"
    )


class KbPermission(Base):
    __tablename__ = "kb_permission"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    kb_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    subject_type: Mapped[str] = mapped_column(String(20), nullable=False)
    subject_id: Mapped[str] = mapped_column(String(50), nullable=False)
    permission: Mapped[str] = mapped_column(String(20), nullable=False)
    granted_by: Mapped[int] = mapped_column(BigInteger, nullable=False)
    granted_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=shanghai_now_naive, server_default=SHANGHAI_NOW_SQL
    )


class KbDocument(Base):
    __tablename__ = "kb_document"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    kb_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    file_name: Mapped[str] = mapped_column(String(255), nullable=False)
    file_type: Mapped[str] = mapped_column(String(20), nullable=False)
    file_size: Mapped[int] = mapped_column(BigInteger, nullable=False)
    minio_path: Mapped[str] = mapped_column(String(500), nullable=False)
    status: Mapped[str] = mapped_column(
        String(20), nullable=False, default=DocumentStatus.PENDING.value, server_default="PENDING"
    )
    error_msg: Mapped[str | None] = mapped_column(Text)
    chunk_count: Mapped[int | None] = mapped_column(Integer, default=0, server_default="0")
    token_count: Mapped[int | None] = mapped_column(Integer, default=0, server_default="0")
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1, server_default="1")
    uploaded_by: Mapped[int] = mapped_column(BigInteger, nullable=False)
    uploaded_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=shanghai_now_naive, server_default=SHANGHAI_NOW_SQL
    )
    indexed_at: Mapped[datetime | None] = mapped_column(DateTime)
    is_deleted: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false"
    )


class DocChunk(Base):
    __tablename__ = "kb_doc_chunk"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    doc_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    kb_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    chunk_index: Mapped[int] = mapped_column(Integer, nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    content_tsv: Mapped[str | None] = mapped_column(TSVector)
    embedding: Mapped[list[float]] = mapped_column(PGVector(1024), nullable=False)
    page_num: Mapped[int | None] = mapped_column(Integer)
    section_title: Mapped[str | None] = mapped_column(String(500))
    token_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    doc_version: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=shanghai_now_naive, server_default=SHANGHAI_NOW_SQL
    )


class IndexTask(Base):
    __tablename__ = "kb_index_task"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    doc_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    task_type: Mapped[str] = mapped_column(
        String(20), nullable=False, default=IndexTaskType.INDEX.value, server_default="INDEX"
    )
    status: Mapped[str] = mapped_column(
        String(20),
        nullable=False,
        default=IndexTaskStatus.PENDING.value,
        server_default="PENDING",
    )
    retry_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    max_retry: Mapped[int] = mapped_column(Integer, nullable=False, default=3, server_default="3")
    payload: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    error_msg: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=shanghai_now_naive, server_default=SHANGHAI_NOW_SQL
    )
    started_at: Mapped[datetime | None] = mapped_column(DateTime)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime)

    def can_retry(self) -> bool:
        return self.retry_count < self.max_retry and self.status == IndexTaskStatus.FAILED


class ChatSession(Base):
    __tablename__ = "kb_chat_session"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    user_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    kb_ids: Mapped[str] = mapped_column(Text, nullable=False)
    title: Mapped[str | None] = mapped_column(String(200))
    message_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=shanghai_now_naive, server_default=SHANGHAI_NOW_SQL
    )
    last_active_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=shanghai_now_naive, server_default=SHANGHAI_NOW_SQL
    )
    is_deleted: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false"
    )


class ChatMessage(Base):
    __tablename__ = "kb_chat_message"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    session_id: Mapped[str] = mapped_column(String(36), nullable=False)
    role: Mapped[str] = mapped_column(String(20), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    sources: Mapped[dict[str, Any] | list[dict[str, Any]] | None] = mapped_column(JSONB)
    token_count: Mapped[int | None] = mapped_column(Integer, default=0, server_default="0")
    latency_ms: Mapped[int | None] = mapped_column(Integer, default=0, server_default="0")
    feedback: Mapped[int | None] = mapped_column(SmallInteger)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=shanghai_now_naive, server_default=SHANGHAI_NOW_SQL
    )


class AnswerFeedback(Base):
    __tablename__ = "kb_answer_feedback"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    message_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    user_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    feedback: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    comment: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=shanghai_now_naive, server_default=SHANGHAI_NOW_SQL
    )


class EvalDataset(Base):
    __tablename__ = "kb_eval_dataset"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    kb_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    question: Mapped[str] = mapped_column(Text, nullable=False)
    expected_answer: Mapped[str | None] = mapped_column(Text)
    expected_chunk_ids: Mapped[list[int] | None] = mapped_column(ARRAY(BigInteger))
    created_by: Mapped[int] = mapped_column(BigInteger, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=shanghai_now_naive, server_default=SHANGHAI_NOW_SQL
    )


class EvalResult(Base):
    __tablename__ = "kb_eval_result"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    dataset_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    eval_version: Mapped[str] = mapped_column(String(50), nullable=False)
    hit: Mapped[bool] = mapped_column(Boolean, nullable=False)
    rank: Mapped[int | None] = mapped_column(Integer)
    actual_answer: Mapped[str | None] = mapped_column(Text)
    faithfulness: Mapped[float | None] = mapped_column(Float)
    answer_relevancy: Mapped[float | None] = mapped_column(Float)
    eval_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=shanghai_now_naive, server_default=SHANGHAI_NOW_SQL
    )
