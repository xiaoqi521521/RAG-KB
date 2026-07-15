-- 扩展现有逐题评估结果；同步运行仍沿用该表，不新增运行表。
ALTER TABLE kb_eval_result
    ALTER COLUMN hit DROP NOT NULL,
    ADD COLUMN IF NOT EXISTS context_recall FLOAT,
    ADD COLUMN IF NOT EXISTS context_precision FLOAT,
    ADD COLUMN IF NOT EXISTS status VARCHAR(20) DEFAULT 'SUCCESS',
    ADD COLUMN IF NOT EXISTS error_type VARCHAR(100);

UPDATE kb_eval_result
SET status = 'SUCCESS'
WHERE status IS NULL;

ALTER TABLE kb_eval_result
    ALTER COLUMN status SET NOT NULL,
    ALTER COLUMN status DROP DEFAULT;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'uq_eval_result_dataset_version'
          AND conrelid = 'kb_eval_result'::regclass
    ) THEN
        ALTER TABLE kb_eval_result
            ADD CONSTRAINT uq_eval_result_dataset_version UNIQUE (dataset_id, eval_version);
    END IF;
END
$$;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'ck_eval_result_status'
          AND conrelid = 'kb_eval_result'::regclass
    ) THEN
        ALTER TABLE kb_eval_result
            ADD CONSTRAINT ck_eval_result_status
            CHECK (status IN ('SUCCESS', 'PARTIAL', 'FAILED'));
    END IF;
END
$$;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'ck_eval_result_rank'
          AND conrelid = 'kb_eval_result'::regclass
    ) THEN
        ALTER TABLE kb_eval_result
            ADD CONSTRAINT ck_eval_result_rank
            CHECK (rank IS NULL OR rank BETWEEN 1 AND 5);
    END IF;
END
$$;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'ck_eval_result_scores'
          AND conrelid = 'kb_eval_result'::regclass
    ) THEN
        ALTER TABLE kb_eval_result
            ADD CONSTRAINT ck_eval_result_scores CHECK (
                (faithfulness IS NULL OR faithfulness BETWEEN 0.0 AND 1.0)
                AND (answer_relevancy IS NULL OR answer_relevancy BETWEEN 0.0 AND 1.0)
                AND (context_recall IS NULL OR context_recall BETWEEN 0.0 AND 1.0)
                AND (context_precision IS NULL OR context_precision BETWEEN 0.0 AND 1.0)
            );
    END IF;
END
$$;
