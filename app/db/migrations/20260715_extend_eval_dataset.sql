-- 扩展现有标准问题集；不新增评估相关数据表。
ALTER TABLE kb_eval_dataset
    ADD COLUMN IF NOT EXISTS status VARCHAR(20) DEFAULT 'ACTIVE',
    ADD COLUMN IF NOT EXISTS review_reason VARCHAR(50),
    ADD COLUMN IF NOT EXISTS source_feedback_id BIGINT;

UPDATE kb_eval_dataset
SET status = 'ACTIVE'
WHERE status IS NULL;

ALTER TABLE kb_eval_dataset
    ALTER COLUMN status SET DEFAULT 'ACTIVE',
    ALTER COLUMN status SET NOT NULL;

CREATE INDEX IF NOT EXISTS idx_eval_dataset_kb_status
    ON kb_eval_dataset(kb_id, status);

CREATE UNIQUE INDEX IF NOT EXISTS uq_eval_dataset_source_feedback
    ON kb_eval_dataset(source_feedback_id)
    WHERE source_feedback_id IS NOT NULL;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1
        FROM pg_constraint
        WHERE conname = 'ck_eval_dataset_status'
    ) THEN
        ALTER TABLE kb_eval_dataset
            ADD CONSTRAINT ck_eval_dataset_status
            CHECK (status IN ('CANDIDATE', 'ACTIVE', 'NEEDS_REVIEW', 'ARCHIVED'));
    END IF;
END
$$;
