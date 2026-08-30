-- 全量导出文件依赖的扩展、序列和触发器函数。
CREATE EXTENSION IF NOT EXISTS vector;

CREATE SEQUENCE IF NOT EXISTS kb_answer_feedback_id_seq;
CREATE SEQUENCE IF NOT EXISTS kb_chat_message_id_seq;
CREATE SEQUENCE IF NOT EXISTS kb_doc_chunk_id_seq;
CREATE SEQUENCE IF NOT EXISTS kb_document_id_seq;
CREATE SEQUENCE IF NOT EXISTS kb_eval_dataset_id_seq;
CREATE SEQUENCE IF NOT EXISTS kb_eval_result_id_seq;
CREATE SEQUENCE IF NOT EXISTS kb_index_task_id_seq;
CREATE SEQUENCE IF NOT EXISTS kb_knowledge_base_id_seq;
CREATE SEQUENCE IF NOT EXISTS kb_permission_id_seq;

CREATE OR REPLACE FUNCTION update_chunk_tsv()
RETURNS TRIGGER AS $$
BEGIN
    NEW.content_tsv := to_tsvector('simple', NEW.content);
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;
