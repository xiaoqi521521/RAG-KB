-- 将历史取消反馈从 NULL 归一化为 0，并恢复反馈字段的非空约束。
ALTER TABLE kb_answer_feedback
    DROP CONSTRAINT IF EXISTS ck_answer_feedback_value;

UPDATE kb_answer_feedback
SET feedback = 0
WHERE feedback IS NULL;

ALTER TABLE kb_answer_feedback
    ALTER COLUMN feedback SET NOT NULL;

ALTER TABLE kb_answer_feedback
    ADD CONSTRAINT ck_answer_feedback_value
    CHECK (feedback IN (-1, 0, 1));
