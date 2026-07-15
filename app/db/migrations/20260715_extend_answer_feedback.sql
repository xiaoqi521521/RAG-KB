-- 保存助手消息的实际知识库范围，并约束现有反馈取值。
ALTER TABLE kb_chat_message
    ADD COLUMN IF NOT EXISTS kb_ids BIGINT[];

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'ck_answer_feedback_value'
          AND conrelid = 'kb_answer_feedback'::regclass
    ) THEN
        ALTER TABLE kb_answer_feedback
            ADD CONSTRAINT ck_answer_feedback_value
            CHECK (feedback IN (-1, 1));
    END IF;
END
$$;
