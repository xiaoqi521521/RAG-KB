-- 为同步评估报告补充整轮耗时；旧数据没有耗时观测时保持 NULL。
ALTER TABLE kb_eval_result
    ADD COLUMN IF NOT EXISTS duration_ms INTEGER;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'ck_eval_result_duration'
          AND conrelid = 'kb_eval_result'::regclass
    ) THEN
        ALTER TABLE kb_eval_result
            ADD CONSTRAINT ck_eval_result_duration
            CHECK (duration_ms IS NULL OR duration_ms >= 0);
    END IF;
END
$$;

COMMENT ON COLUMN kb_eval_result.duration_ms IS '整轮评估执行耗时，单位为毫秒；历史数据可为空';
