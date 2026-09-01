-- 取消反馈时保留原记录，避免破坏差评候选的来源追溯。
ALTER TABLE kb_answer_feedback
    ALTER COLUMN feedback DROP NOT NULL;

ALTER TABLE kb_answer_feedback
    DROP CONSTRAINT IF EXISTS ck_answer_feedback_value;

ALTER TABLE kb_answer_feedback
    ADD CONSTRAINT ck_answer_feedback_value
    CHECK (feedback IS NULL OR feedback IN (-1, 1));
