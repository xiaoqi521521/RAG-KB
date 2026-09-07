from pathlib import Path
from datetime import datetime
import re
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


def test_all_database_columns_have_comments_in_schema_and_migration():
    """数据库元数据注释应覆盖 ORM 映射的全部业务字段。"""
    from app.core.database import Base
    import app.models  # noqa: F401

    expected_columns = {
        (table.name, column.name)
        for table in Base.metadata.tables.values()
        for column in table.columns
        if table.name.startswith("kb_")
    }

    for sql_path in (
        Path("app/db/schema.sql"),
        Path("app/db/migrations/20260903_add_column_comments.sql"),
    ):
        commented_columns = set(
            re.findall(
                r"COMMENT ON COLUMN (kb_[a-z_]+)\.([a-z_]+) IS",
                sql_path.read_text(encoding="utf-8"),
            )
        )
        assert expected_columns <= commented_columns


def test_status_column_comments_explain_each_enum_value():
    """多枚举状态注释必须同时给出每个值的业务含义。"""
    expected_comments = {
        "kb_document.status": (
            "PENDING=待处理",
            "PROCESSING=处理中",
            "DONE=索引完成",
            "FAILED=索引失败",
        ),
        "kb_index_task.status": (
            "PENDING=待执行",
            "RUNNING=执行中",
            "DONE=执行完成",
            "FAILED=执行失败",
        ),
        "kb_eval_dataset.status": (
            "CANDIDATE=候选待审核",
            "ACTIVE=有效并纳入评估",
            "NEEDS_REVIEW=关联内容变更后待复核",
            "ARCHIVED=已归档且不纳入默认评估",
        ),
        "kb_eval_result.status": (
            "SUCCESS=评估成功",
            "PARTIAL=部分完成或发生降级",
            "FAILED=评估失败",
        ),
    }

    for sql_path in (
        Path("app/db/schema.sql"),
        Path("app/db/migrations/20260903_add_column_comments.sql"),
    ):
        sql = sql_path.read_text(encoding="utf-8")
        for column, meanings in expected_comments.items():
            table, column_name = column.split(".")
            marker = f"COMMENT ON COLUMN {table}.{column_name} IS '"
            start = sql.index(marker) + len(marker)
            comment = sql[start : sql.index("';", start)]
            assert all(meaning in comment for meaning in meanings)


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
        (AnswerFeedback, "updated_at"),
        (EvalDataset, "created_at"),
        (EvalDataset, "updated_at"),
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


def test_eval_dataset_model_and_sql_define_lifecycle_constraints():
    from app.models import EvalDataset, EvalDatasetStatus

    assert {status.value for status in EvalDatasetStatus} == {
        "CANDIDATE",
        "ACTIVE",
        "NEEDS_REVIEW",
        "ARCHIVED",
    }
    columns = EvalDataset.__table__.columns
    assert columns["status"].default.arg == "ACTIVE"
    assert columns["status"].nullable is False
    assert columns["review_reason"].nullable is True
    assert columns["source_feedback_id"].nullable is True
    assert columns["source_feedback_id"].unique is True
    assert columns["updated_at"].nullable is False
    assert columns["updated_at"].onupdate.arg.__name__ == "shanghai_now_naive"
    assert {index.name for index in EvalDataset.__table__.indexes} >= {
        "idx_eval_dataset_kb_status"
    }

    schema_sql = Path("app/db/schema.sql").read_text(encoding="utf-8")
    assert "status              VARCHAR(20)     NOT NULL DEFAULT 'ACTIVE'" in schema_sql
    assert "review_reason       VARCHAR(50)" in schema_sql
    assert "source_feedback_id  BIGINT UNIQUE" in schema_sql
    assert "updated_at      TIMESTAMP       NOT NULL" in schema_sql
    assert "trigger_eval_dataset_updated_at" in schema_sql
    assert "idx_eval_dataset_kb_status" in schema_sql
    assert [column.name for column in EvalDataset.__table__.columns] == [
        "id",
        "kb_id",
        "question",
        "expected_answer",
        "expected_chunk_ids",
        "created_by",
        "status",
        "review_reason",
        "source_feedback_id",
        "created_at",
        "updated_at",
    ]

    migration_sql = Path(
        "app/db/migrations/20260715_extend_eval_dataset.sql"
    ).read_text(encoding="utf-8")
    assert "ADD COLUMN IF NOT EXISTS status" in migration_sql
    assert "UPDATE kb_eval_dataset" in migration_sql
    assert "SET status = 'ACTIVE'" in migration_sql

    updated_at_migration_sql = Path(
        "app/db/migrations/20260903_add_eval_dataset_updated_at.sql"
    ).read_text(encoding="utf-8")
    assert "ADD COLUMN IF NOT EXISTS updated_at TIMESTAMP" in updated_at_migration_sql
    assert "SET updated_at = COALESCE(updated_at, created_at" in updated_at_migration_sql
    assert "CREATE TRIGGER trigger_eval_dataset_updated_at" in updated_at_migration_sql
    reorder_migration_sql = Path(
        "app/db/migrations/20260903_reorder_eval_dataset_columns.sql"
    ).read_text(encoding="utf-8")
    assert "source_feedback_id, created_at, updated_at" in reorder_migration_sql
    assert "ALTER TABLE kb_eval_dataset_reordered RENAME TO kb_eval_dataset" in reorder_migration_sql


def test_eval_result_model_and_sql_define_metric_constraints():
    from sqlalchemy.dialects import postgresql

    from app.models import EvalResult, EvalResultStatus

    assert {status.value for status in EvalResultStatus} == {
        "SUCCESS",
        "PARTIAL",
        "FAILED",
    }
    columns = EvalResult.__table__.columns
    assert columns["eval_version"].type.compile(dialect=postgresql.dialect()) == "INTEGER"
    assert [column.name for column in columns] == [
        "id",
        "dataset_id",
        "eval_version",
        "actual_answer",
        "hit",
        "rank",
        "faithfulness",
        "answer_relevancy",
        "context_recall",
        "context_precision",
        "status",
        "error_type",
        "duration_ms",
        "eval_at",
    ]
    assert columns["hit"].nullable is True
    assert columns["rank"].nullable is True
    assert columns["context_recall"].nullable is True
    assert columns["context_precision"].nullable is True
    assert columns["status"].nullable is False
    assert columns["error_type"].nullable is True
    assert columns["duration_ms"].nullable is True
    assert {constraint.name for constraint in EvalResult.__table__.constraints} >= {
        "uq_eval_result_dataset_version",
        "ck_eval_result_status",
        "ck_eval_result_rank",
        "ck_eval_result_scores",
        "ck_eval_result_duration",
    }

    schema_sql = Path("app/db/schema.sql").read_text(encoding="utf-8")
    assert "hit             BOOLEAN" in schema_sql
    assert "context_recall  FLOAT" in schema_sql
    assert "context_precision FLOAT" in schema_sql
    assert "status              VARCHAR(20)     NOT NULL" in schema_sql
    assert "error_type          VARCHAR(100)" in schema_sql
    assert "duration_ms         INTEGER" in schema_sql
    assert "uq_eval_result_dataset_version" in schema_sql
    assert schema_sql.index("eval_version") < schema_sql.index("actual_answer")
    assert schema_sql.index("actual_answer") < schema_sql.index("hit")
    assert schema_sql.index("error_type") < schema_sql.index("eval_at")

    duration_migration_sql = Path(
        "app/db/migrations/20260907_add_eval_duration.sql"
    ).read_text(encoding="utf-8")
    assert "ADD COLUMN IF NOT EXISTS duration_ms INTEGER" in duration_migration_sql
    assert "ck_eval_result_duration" in duration_migration_sql

    migration_sql = Path("app/db/migrations/20260715_extend_eval_result.sql").read_text(
        encoding="utf-8"
    )
    assert "ALTER COLUMN hit DROP NOT NULL" in migration_sql
    assert "ADD COLUMN IF NOT EXISTS context_recall" in migration_sql
    assert "uq_eval_result_dataset_version" in migration_sql

    reorder_migration_sql = Path(
        "app/db/migrations/20260903_reorder_eval_result_columns.sql"
    ).read_text(encoding="utf-8")
    assert "eval_version', 'actual_answer', 'hit'" in reorder_migration_sql
    assert "'error_type', 'eval_at'" in reorder_migration_sql
    assert "ALTER TABLE kb_eval_result_reordered RENAME TO kb_eval_result" in reorder_migration_sql

    dump_sql = Path("app/db/ragkb_full_dump.sql").read_text(encoding="utf-8")
    assert '"eval_version" int4 NOT NULL' in dump_sql
    assert dump_sql.index('"eval_version" int4 NOT NULL') < dump_sql.index('"actual_answer" text')
    assert dump_sql.index('"actual_answer" text') < dump_sql.index('"hit" bool')
    assert dump_sql.index('"error_type" varchar(100)') < dump_sql.index('"eval_at" timestamp')
    assert 'INSERT INTO "public"."kb_eval_result" (id, dataset_id, eval_version, hit, rank, actual_answer' in dump_sql


def test_feedback_models_and_sql_define_scope_and_integrity_constraints():
    from sqlalchemy.dialects import postgresql

    from app.models import AnswerFeedback, ChatMessage

    assert ChatMessage.__table__.columns["kb_ids"].nullable is True
    assert (
        ChatMessage.__table__.columns["kb_ids"].type.compile(dialect=postgresql.dialect())
        == "BIGINT[]"
    )
    assert AnswerFeedback.__table__.columns["feedback"].nullable is False
    feedback_columns = AnswerFeedback.__table__.columns
    assert feedback_columns["updated_at"].nullable is False
    assert feedback_columns["updated_at"].onupdate.arg.__name__ == "shanghai_now_naive"
    assert {constraint.name for constraint in AnswerFeedback.__table__.constraints} >= {
        "uq_answer_feedback_message_user",
        "ck_answer_feedback_value",
    }

    schema_sql = Path("app/db/schema.sql").read_text(encoding="utf-8")
    assert "kb_ids          BIGINT[]" in schema_sql
    assert "uq_answer_feedback_message_user" in schema_sql
    assert "feedback        SMALLINT        NOT NULL" in schema_sql
    assert "feedback IN (-1, 0, 1)" in schema_sql
    assert "updated_at      TIMESTAMP       NOT NULL" in schema_sql
    assert "trigger_answer_feedback_updated_at" in schema_sql

    dump_sql = Path("app/db/ragkb_full_dump.sql").read_text(encoding="utf-8")
    assert '"feedback" int2 NOT NULL' in dump_sql
    assert "feedback = ANY (ARRAY['-1'::integer, 0, 1])" in dump_sql

    migration_sql = Path(
        "app/db/migrations/20260715_extend_answer_feedback.sql"
    ).read_text(encoding="utf-8")
    assert "ADD COLUMN IF NOT EXISTS kb_ids BIGINT[]" in migration_sql
    assert "ck_answer_feedback_value" in migration_sql

    cancellation_migration_sql = Path(
        "app/db/migrations/20260901_allow_feedback_cancellation.sql"
    ).read_text(encoding="utf-8")
    assert "ALTER COLUMN feedback DROP NOT NULL" in cancellation_migration_sql
    assert "feedback IS NULL OR feedback IN (-1, 1)" in cancellation_migration_sql

    zero_state_migration_sql = Path(
        "app/db/migrations/20260902_store_cancelled_feedback_as_zero.sql"
    ).read_text(encoding="utf-8")
    assert "DROP CONSTRAINT IF EXISTS ck_answer_feedback_value" in zero_state_migration_sql
    assert "SET feedback = 0" in zero_state_migration_sql
    assert "ALTER COLUMN feedback SET NOT NULL" in zero_state_migration_sql
    assert "feedback IN (-1, 0, 1)" in zero_state_migration_sql

    updated_at_migration_sql = Path(
        "app/db/migrations/20260903_add_answer_feedback_updated_at.sql"
    ).read_text(encoding="utf-8")
    assert "ADD COLUMN IF NOT EXISTS updated_at TIMESTAMP" in updated_at_migration_sql
    assert "SET updated_at = COALESCE(updated_at, created_at" in updated_at_migration_sql
    assert "CREATE TRIGGER trigger_answer_feedback_updated_at" in updated_at_migration_sql

    assert [column.name for column in AnswerFeedback.__table__.columns] == [
        "id",
        "message_id",
        "user_id",
        "feedback",
        "comment",
        "created_at",
        "updated_at",
    ]


def test_chat_intent_metadata_is_present_in_schema_and_migration():
    """意图路由依赖的消息元数据必须同时存在于模型快照和增量迁移。"""
    from app.models import ChatMessage

    columns = ChatMessage.__table__.columns
    assert columns["answer_mode"].nullable is False
    assert columns["knowledge_base_searched"].nullable is False

    schema_sql = Path("app/db/schema.sql").read_text(encoding="utf-8")
    assert "answer_mode     VARCHAR(30) NOT NULL DEFAULT 'knowledge_base'" in schema_sql
    assert "knowledge_base_searched BOOLEAN NOT NULL DEFAULT TRUE" in schema_sql

    migration_sql = Path(
        "app/db/migrations/20260902_add_chat_intent_metadata.sql"
    ).read_text(encoding="utf-8")
    assert "ADD COLUMN IF NOT EXISTS answer_mode" in migration_sql
    assert "ADD COLUMN IF NOT EXISTS knowledge_base_searched" in migration_sql


def test_chat_message_columns_keep_created_at_last_across_schema_dump_and_migration():
    """用户消息表的创建时间字段应在消息业务字段之后。"""
    from app.models import ChatMessage

    expected_columns = [
        "id",
        "session_id",
        "role",
        "content",
        "sources",
        "token_count",
        "latency_ms",
        "feedback",
        "kb_ids",
        "answer_mode",
        "knowledge_base_searched",
        "created_at",
    ]
    assert [column.name for column in ChatMessage.__table__.columns] == expected_columns

    schema_sql = Path("app/db/schema.sql").read_text(encoding="utf-8")
    schema_start = schema_sql.index("CREATE TABLE kb_chat_message")
    schema_end = schema_sql.index("\n);", schema_start)
    schema_table = schema_sql[schema_start:schema_end]
    assert schema_table.index("knowledge_base_searched") < schema_table.index("created_at")

    dump_sql = Path("app/db/ragkb_full_dump.sql").read_text(encoding="utf-8")
    dump_start = dump_sql.index('CREATE TABLE "public"."kb_chat_message"')
    dump_end = dump_sql.index("\n;", dump_start)
    dump_table = dump_sql[dump_start:dump_end]
    assert dump_table.index('"knowledge_base_searched"') < dump_table.index('"created_at"')
    assert 'INSERT INTO "public"."kb_chat_message" (id, session_id, role, content, sources, token_count, latency_ms, feedback, created_at, kb_ids)' in dump_sql
    assert 'INSERT INTO "public"."kb_chat_message" VALUES' not in dump_sql

    migration_sql = Path(
        "app/db/migrations/20260903_reorder_chat_message_columns.sql"
    ).read_text(encoding="utf-8")
    assert "ADD COLUMN IF NOT EXISTS answer_mode" in migration_sql
    assert "ADD COLUMN IF NOT EXISTS knowledge_base_searched" in migration_sql
    assert "'knowledge_base_searched', 'created_at'" in migration_sql
    assert "ALTER TABLE kb_chat_message_reordered RENAME TO kb_chat_message" in migration_sql
    assert "CREATE INDEX idx_message_session ON kb_chat_message(session_id, created_at)" in migration_sql
