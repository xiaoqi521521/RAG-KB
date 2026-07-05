from pathlib import Path


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


def test_index_task_can_retry_only_failed_tasks_under_limit():
    from app.models import IndexTask, IndexTaskStatus

    failed_task = IndexTask(status=IndexTaskStatus.FAILED, retry_count=2, max_retry=3)
    exhausted_task = IndexTask(status=IndexTaskStatus.FAILED, retry_count=3, max_retry=3)
    pending_task = IndexTask(status=IndexTaskStatus.PENDING, retry_count=0, max_retry=3)

    assert failed_task.can_retry() is True
    assert exhausted_task.can_retry() is False
    assert pending_task.can_retry() is False
