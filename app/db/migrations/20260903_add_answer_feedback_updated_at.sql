-- 为用户回答反馈增加更新时间，并确保任意 UPDATE 都会自动刷新该字段。
ALTER TABLE kb_answer_feedback
    ADD COLUMN IF NOT EXISTS updated_at TIMESTAMP;

UPDATE kb_answer_feedback
SET updated_at = COALESCE(updated_at, created_at, timezone('Asia/Shanghai', now()));

ALTER TABLE kb_answer_feedback
    ALTER COLUMN updated_at SET DEFAULT timezone('Asia/Shanghai', now()),
    ALTER COLUMN updated_at SET NOT NULL;

CREATE OR REPLACE FUNCTION update_answer_feedback_updated_at()
RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at := timezone('Asia/Shanghai', now());
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trigger_answer_feedback_updated_at ON kb_answer_feedback;
CREATE TRIGGER trigger_answer_feedback_updated_at
    BEFORE UPDATE ON kb_answer_feedback
    FOR EACH ROW
    EXECUTE FUNCTION update_answer_feedback_updated_at();

COMMENT ON COLUMN kb_answer_feedback.updated_at IS '最后更新时间，反馈更新后自动刷新';
