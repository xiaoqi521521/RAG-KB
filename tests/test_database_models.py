from pathlib import Path
from datetime import datetime
from zoneinfo import ZoneInfo


def test_schema_sql_exists_with_expected_tables_and_pgvector_extension():
    schema_path = Path("app/db/schema.sql")

    assert schema_path.exists()

    schema_sql = schema_path.read_text(encoding="utf-8")

    assert "CREATE EXTENSION IF NOT EXISTS vector;" in schema_sql
    for table_name in (
        "kb_knowledge_base",
        "kb_permission",
        "kb_document",
        "kb_doc_chunk",
        "kb_index_task",
        "kb_chat_session",
        "kb_chat_message",
        "kb_answer_feedback",
        "kb_eval_dataset",
        "kb_eval_result",
    ):
        assert f"CREATE TABLE {table_name}" in schema_sql


def test_schema_sql_uses_shanghai_timestamp_defaults():
    schema_sql = Path("app/db/schema.sql").read_text(encoding="utf-8")

    assert "DEFAULT NOW()" not in schema_sql
    assert "DEFAULT timezone('Asia/Shanghai', now())" in schema_sql


def test_database_models_are_registered_on_base_metadata():
    from app.core.database import Base
    import app.models  # noqa: F401

    expected_tables = {
        "kb_knowledge_base",
        "kb_permission",
        "kb_document",
        "kb_doc_chunk",
        "kb_index_task",
        "kb_chat_session",
        "kb_chat_message",
        "kb_answer_feedback",
        "kb_eval_dataset",
        "kb_eval_result",
    }

    assert expected_tables.issubset(Base.metadata.tables.keys())

    chunk_columns = Base.metadata.tables["kb_doc_chunk"].columns
    assert chunk_columns["embedding"].type.compile() == "VECTOR(1024)"
    assert chunk_columns["embedding"].type.bind_processor(None) is not None
    assert chunk_columns["content_tsv"].type.compile(dialect=None) == "TSVECTOR"
    assert "payload" in Base.metadata.tables["kb_index_task"].columns


def test_timestamp_columns_use_shanghai_application_defaults():
    from app.models import (
        AnswerFeedback,
        ChatMessage,
        ChatSession,
        DocChunk,
        EvalDataset,
        EvalResult,
        IndexTask,
        KbDocument,
        KbPermission,
        KnowledgeBase,
    )

    timestamp_columns = [
        (KnowledgeBase, "created_at"),
        (KnowledgeBase, "updated_at"),
        (KbPermission, "granted_at"),
        (KbDocument, "uploaded_at"),
        (DocChunk, "created_at"),
        (IndexTask, "created_at"),
        (ChatSession, "created_at"),
        (ChatSession, "last_active_at"),
        (ChatMessage, "created_at"),
        (AnswerFeedback, "created_at"),
        (EvalDataset, "created_at"),
        (EvalResult, "eval_at"),
    ]

    for model, column_name in timestamp_columns:
        column = model.__table__.columns[column_name]
        assert column.default.arg.__name__ == "shanghai_now_naive"
        assert "Asia/Shanghai" in str(column.server_default.arg)

        before = datetime.now(ZoneInfo("Asia/Shanghai")).replace(tzinfo=None)
        current = column.default.arg(None)
        after = datetime.now(ZoneInfo("Asia/Shanghai")).replace(tzinfo=None)
        assert current.tzinfo is None
        assert before <= current <= after


def test_index_task_can_retry_only_failed_tasks_under_limit():
    from app.models import IndexTask, IndexTaskStatus

    failed_task = IndexTask(status=IndexTaskStatus.FAILED, retry_count=2, max_retry=3)
    exhausted_task = IndexTask(status=IndexTaskStatus.FAILED, retry_count=3, max_retry=3)
    pending_task = IndexTask(status=IndexTaskStatus.PENDING, retry_count=0, max_retry=3)

    assert failed_task.can_retry() is True
    assert exhausted_task.can_retry() is False
    assert pending_task.can_retry() is False
