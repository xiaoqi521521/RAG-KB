from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import datetime

import pytest
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncSession, create_async_engine
from sqlalchemy.pool import NullPool

from app.core.config import get_settings
from app.models import ChatMessageRole, ChatSession
from app.repositories.chat import ChatRepository


class FakeResult:
    def __init__(self, scalar: object = None) -> None:
        self.scalar = scalar

    def scalar_one_or_none(self):
        return self.scalar

    def scalars(self):
        return []


class FakeSession:
    def __init__(self, results: list[FakeResult] | None = None) -> None:
        self.results = results or []
        self.statements: list[object] = []
        self.added: list[object] = []
        self.flush_count = 0

    async def execute(self, statement):
        self.statements.append(statement)
        return self.results.pop(0) if self.results else FakeResult()

    def add_all(self, values: list[object]) -> None:
        self.added.extend(values)

    async def flush(self) -> None:
        self.flush_count += 1


@asynccontextmanager
async def _postgres_transaction() -> AsyncIterator[AsyncConnection]:
    """提供使用临时表且最终整体回滚的 PostgreSQL 测试事务。"""
    engine = create_async_engine(get_settings().database_url, poolclass=NullPool)
    try:
        connection = await engine.connect()
    except (OSError, SQLAlchemyError) as exc:
        await engine.dispose()
        pytest.skip(f"PostgreSQL test database unavailable: {type(exc).__name__}")

    transaction = await connection.begin()
    try:
        yield connection
    finally:
        if transaction.is_active:
            await transaction.rollback()
        await connection.close()
        await engine.dispose()


async def test_owned_session_and_messages_queries_filter_by_user_and_active_status() -> None:
    session = FakeSession()
    repository = ChatRepository(session)  # type: ignore[arg-type]

    await repository.get_active_session_for_user("session-1", 7)
    await repository.list_messages_for_user("session-1", 7)

    active_session_sql = str(session.statements[0])
    messages_sql = str(session.statements[1])
    assert "kb_chat_session.user_id = :user_id_1" in active_session_sql
    assert "kb_chat_session.is_deleted IS false" in active_session_sql
    assert "kb_chat_message.session_id = :session_id_1" in messages_sql
    assert "kb_chat_session.user_id = :user_id_1" in messages_sql
    assert "kb_chat_session.is_deleted IS false" in messages_sql


async def test_soft_delete_session_filters_by_owner_and_active_status() -> None:
    session = FakeSession([FakeResult("session-1")])
    repository = ChatRepository(session)  # type: ignore[arg-type]

    deleted = await repository.soft_delete_session_for_user("session-1", 7)

    statement_sql = str(session.statements[0])
    assert deleted is True
    assert "UPDATE kb_chat_session SET is_deleted=:is_deleted" in statement_sql
    assert "kb_chat_session.id = :id_1" in statement_sql
    assert "kb_chat_session.user_id = :user_id_1" in statement_sql
    assert "kb_chat_session.is_deleted IS false" in statement_sql
    assert "RETURNING kb_chat_session.id" in statement_sql


async def test_soft_delete_session_updates_only_the_owned_active_session_in_postgres() -> None:
    """真实数据库更新必须保留其他用户会话，并使已删除会话不可读取。"""
    async with _postgres_transaction() as connection:
        await connection.execute(
            text(
                """
                CREATE TEMP TABLE kb_chat_session (
                    id VARCHAR(36) PRIMARY KEY,
                    user_id BIGINT NOT NULL,
                    kb_ids TEXT NOT NULL,
                    title VARCHAR(200),
                    message_count INT NOT NULL DEFAULT 0,
                    created_at TIMESTAMP NOT NULL DEFAULT now(),
                    last_active_at TIMESTAMP NOT NULL DEFAULT now(),
                    is_deleted BOOLEAN NOT NULL DEFAULT FALSE
                ) ON COMMIT DROP
                """
            )
        )
        await connection.execute(
            text(
                """
                INSERT INTO kb_chat_session (id, user_id, kb_ids)
                VALUES
                    ('owned-session', 7, '[2]'),
                    ('other-session', 8, '[2]')
                """
            )
        )
        session = AsyncSession(bind=connection, join_transaction_mode="create_savepoint")
        repository = ChatRepository(session)

        deleted = await repository.soft_delete_session_for_user("owned-session", 7)
        foreign_deleted = await repository.soft_delete_session_for_user("other-session", 7)
        await session.commit()

        session_states = (
            await connection.execute(
                text("SELECT id, is_deleted FROM kb_chat_session ORDER BY id")
            )
        ).all()
        deleted_session = await repository.get_active_session_for_user("owned-session", 7)
        other_session = await repository.get_active_session_for_user("other-session", 8)
        await session.close()

    assert deleted is True
    assert foreign_deleted is False
    assert session_states == [("other-session", False), ("owned-session", True)]
    assert deleted_session is None
    assert other_session is not None


async def test_saved_turn_records_scope_only_on_assistant_message() -> None:
    chat_session = ChatSession(
        id="session-1",
        user_id=7,
        kb_ids="[2, 3]",
        message_count=0,
        created_at=datetime(2026, 7, 15),
        last_active_at=datetime(2026, 7, 15),
    )
    session = FakeSession([FakeResult(chat_session)])
    repository = ChatRepository(session)  # type: ignore[arg-type]

    saved = await repository.add_turn_for_user(
        session_id="session-1",
        user_id=7,
        kb_ids=[2, 3],
        question="问题",
        answer="回答",
        sources=[],
        token_count=3,
        latency_ms=10,
    )

    assert saved is not None
    assert len(session.added) == 2
    user_message, assistant_message = session.added
    assert user_message.role == ChatMessageRole.USER.value  # type: ignore[attr-defined]
    assert user_message.kb_ids is None  # type: ignore[attr-defined]
    assert assistant_message.role == ChatMessageRole.ASSISTANT.value  # type: ignore[attr-defined]
    assert assistant_message.kb_ids == [2, 3]  # type: ignore[attr-defined]
