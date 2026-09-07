-- 为评估结果补充当题模型消耗；旧数据没有计费观测时按 0 处理。
ALTER TABLE kb_eval_result
    ADD COLUMN IF NOT EXISTS usage_tokens INTEGER NOT NULL DEFAULT 0;

ALTER TABLE kb_eval_result
    ADD COLUMN IF NOT EXISTS estimated_cost_cny NUMERIC(12, 6) NOT NULL DEFAULT 0;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'ck_eval_result_usage'
          AND conrelid = 'kb_eval_result'::regclass
    ) THEN
        ALTER TABLE kb_eval_result
            ADD CONSTRAINT ck_eval_result_usage
            CHECK (usage_tokens >= 0 AND estimated_cost_cny >= 0);
    END IF;
END
$$;

COMMENT ON COLUMN kb_eval_result.usage_tokens IS '当题评估消耗的模型 Token 总数';
COMMENT ON COLUMN kb_eval_result.estimated_cost_cny IS '当题评估消耗的估算金额，单位为 CNY';
