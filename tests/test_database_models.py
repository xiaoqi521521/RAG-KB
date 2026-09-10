from datetime import datetime
from zoneinfo import ZoneInfo


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
        "kb_eval_run_usage",
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
        EvalRunUsage,
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
        (EvalRunUsage, "created_at"),
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


def test_eval_dataset_model_defines_lifecycle_constraints():
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
    assert [column.name for column in columns] == [
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


def test_eval_result_model_defines_metric_constraints():
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


def test_eval_run_usage_model_defines_run_level_totals():
    from sqlalchemy.dialects import postgresql

    from app.models import EvalRunUsage

    columns = EvalRunUsage.__table__.columns
    assert [column.name for column in columns] == [
        "id",
        "kb_id",
        "eval_version",
        "generation_usage_tokens",
        "generation_estimated_cost_cny",
        "ragas_input_tokens",
        "ragas_evaluation_tokens",
        "ragas_embedding_tokens",
        "ragas_input_cost_cny",
        "ragas_evaluation_cost_cny",
        "ragas_embedding_cost_cny",
        "usage_tokens",
        "estimated_cost_cny",
        "created_at",
    ]
    assert columns["eval_version"].type.compile(dialect=postgresql.dialect()) == "INTEGER"
    assert columns["usage_tokens"].computed is not None
    assert columns["estimated_cost_cny"].computed is not None
    assert {constraint.name for constraint in EvalRunUsage.__table__.constraints} >= {
        "uq_eval_run_usage_kb_version",
        "ck_eval_run_usage_values",
    }


def test_feedback_models_define_scope_and_integrity_constraints():
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
    assert [column.name for column in feedback_columns] == [
        "id",
        "message_id",
        "user_id",
        "feedback",
        "comment",
        "created_at",
        "updated_at",
    ]


def test_chat_intent_metadata_is_present_in_model():
    from app.models import ChatMessage

    columns = ChatMessage.__table__.columns
    assert columns["answer_mode"].nullable is False
    assert columns["knowledge_base_searched"].nullable is False


def test_chat_message_columns_keep_created_at_last():
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
