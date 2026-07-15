from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import pytest
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncSession, create_async_engine
from sqlalchemy.pool import NullPool

from app.core.config import get_settings
from app.repositories.chunks import ChunkRepository
from app.repositories.evaluations import EvaluationRepository


@asynccontextmanager
async def _postgres_transaction() -> AsyncIterator[AsyncConnection]:
    """提供使用临时表且最终整体回滚的 PostgreSQL 测试事务。"""
    test_engine = create_async_engine(
        get_settings().database_url,
        poolclass=NullPool,
    )
    try:
        connection = await test_engine.connect()
    except (OSError, SQLAlchemyError) as exc:
        await test_engine.dispose()
        pytest.skip(f"PostgreSQL test database unavailable: {type(exc).__name__}")

    transaction = await connection.begin()
    try:
        yield connection
    finally:
        if transaction.is_active:
            await transaction.rollback()
        await connection.close()
        await test_engine.dispose()


async def _create_evaluation_temp_table(connection: AsyncConnection) -> None:
    """创建只包含标注失效所需字段的连接级临时表。"""
    await connection.execute(
        text(
            """
            CREATE TEMP TABLE kb_eval_dataset (
                id BIGINT PRIMARY KEY,
                kb_id BIGINT NOT NULL,
                expected_chunk_ids BIGINT[],
                status VARCHAR(20) NOT NULL,
                review_reason VARCHAR(50)
            ) ON COMMIT DROP
            """
        )
    )


@pytest.mark.asyncio
async def test_reindex_invalidation_updates_only_overlapping_active_rows() -> None:
    """数组重叠更新不得影响不重叠、其他状态或其他知识库的问题。"""
    async with _postgres_transaction() as connection:
        await _create_evaluation_temp_table(connection)
        await connection.execute(
            text(
                """
                INSERT INTO kb_eval_dataset
                    (id, kb_id, expected_chunk_ids, status, review_reason)
                VALUES
                    (1, 3, ARRAY[10, 20]::BIGINT[], 'ACTIVE', NULL),
                    (2, 3, ARRAY[11, 30]::BIGINT[], 'ACTIVE', NULL),
                    (3, 3, ARRAY[99]::BIGINT[], 'ACTIVE', NULL),
                    (4, 3, ARRAY[10]::BIGINT[], 'CANDIDATE', NULL),
                    (5, 3, ARRAY[11]::BIGINT[], 'NEEDS_REVIEW', 'existing_reason'),
                    (6, 4, ARRAY[10]::BIGINT[], 'ACTIVE', NULL)
                """
            )
        )
        session = AsyncSession(bind=connection, join_transaction_mode="create_savepoint")
        repository = EvaluationRepository(session)

        await repository.invalidate_reindexed_chunk_labels(
            kb_id=3,
            old_chunk_ids=[10, 11],
        )
        await session.commit()

        rows = (
            await connection.execute(
                text(
                    "SELECT id, status, review_reason "
                    "FROM kb_eval_dataset ORDER BY id"
                )
            )
        ).all()
        await session.close()

    assert rows == [
        (1, "NEEDS_REVIEW", "document_reindexed"),
        (2, "NEEDS_REVIEW", "document_reindexed"),
        (3, "ACTIVE", None),
        (4, "CANDIDATE", None),
        (5, "NEEDS_REVIEW", "existing_reason"),
        (6, "ACTIVE", None),
    ]


@pytest.mark.asyncio
async def test_publication_changes_roll_back_in_one_database_transaction() -> None:
    """发布失败后文档版本、标注状态和旧 chunk 必须一起恢复。"""
    async with _postgres_transaction() as connection:
        await _create_evaluation_temp_table(connection)
        await connection.execute(
            text(
                """
                CREATE TEMP TABLE kb_document (
                    id BIGINT PRIMARY KEY,
                    version INT NOT NULL
                ) ON COMMIT DROP
                """
            )
        )
        await connection.execute(
            text(
                """
                CREATE TEMP TABLE kb_doc_chunk (
                    id BIGINT PRIMARY KEY,
                    doc_id BIGINT NOT NULL,
                    doc_version INT NOT NULL
                ) ON COMMIT DROP
                """
            )
        )
        await connection.execute(
            text(
                "INSERT INTO kb_document (id, version) VALUES (7, 1)"
            )
        )
        await connection.execute(
            text(
                "INSERT INTO kb_doc_chunk (id, doc_id, doc_version) "
                "VALUES (101, 7, 1), (102, 7, 1)"
            )
        )
        await connection.execute(
            text(
                "INSERT INTO kb_eval_dataset "
                "(id, kb_id, expected_chunk_ids, status, review_reason) "
                "VALUES (1, 3, ARRAY[101]::BIGINT[], 'ACTIVE', NULL)"
            )
        )
        session = AsyncSession(bind=connection, join_transaction_mode="create_savepoint")
        chunk_repository = ChunkRepository(session)
        evaluation_repository = EvaluationRepository(session)

        old_chunk_ids = await chunk_repository.list_older_version_ids(7, 2)
        await session.execute(text("UPDATE kb_document SET version = 2 WHERE id = 7"))
        await evaluation_repository.invalidate_reindexed_chunk_labels(
            kb_id=3,
            old_chunk_ids=old_chunk_ids,
        )
        await chunk_repository.delete_older_versions(7, 2)
        await session.rollback()

        document_version = (
            await connection.execute(text("SELECT version FROM kb_document WHERE id = 7"))
        ).scalar_one()
        dataset = (
            await connection.execute(
                text("SELECT status, review_reason FROM kb_eval_dataset WHERE id = 1")
            )
        ).one()
        chunk_ids = list(
            (
                await connection.execute(
                    text("SELECT id FROM kb_doc_chunk WHERE doc_id = 7 ORDER BY id")
                )
            ).scalars()
        )
        await session.close()

    assert document_version == 1
    assert dataset == ("ACTIVE", None)
    assert chunk_ids == [101, 102]
