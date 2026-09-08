-- 将评估用量从逐题结果迁移到 run 级事实表，避免 run 级 RAGAS 成本污染逐题记录。
CREATE TABLE IF NOT EXISTS kb_eval_run_usage (
    id                          BIGSERIAL PRIMARY KEY,
    kb_id                       BIGINT          NOT NULL,
    eval_version                INTEGER         NOT NULL,
    generation_usage_tokens     INTEGER         NOT NULL,
    generation_estimated_cost_cny NUMERIC(12, 6) NOT NULL,
    ragas_input_tokens          INTEGER         NOT NULL,
    ragas_evaluation_tokens     INTEGER         NOT NULL,
    ragas_embedding_tokens      INTEGER         NOT NULL,
    ragas_input_cost_cny        NUMERIC(12, 6)  NOT NULL,
    ragas_evaluation_cost_cny   NUMERIC(12, 6)  NOT NULL,
    ragas_embedding_cost_cny    NUMERIC(12, 6)  NOT NULL,
    usage_tokens                INTEGER GENERATED ALWAYS AS (
        generation_usage_tokens + ragas_input_tokens
        + ragas_evaluation_tokens + ragas_embedding_tokens
    ) STORED,
    estimated_cost_cny          NUMERIC(12, 6) GENERATED ALWAYS AS (
        generation_estimated_cost_cny + ragas_input_cost_cny
        + ragas_evaluation_cost_cny + ragas_embedding_cost_cny
    ) STORED,
    created_at                  TIMESTAMP       NOT NULL DEFAULT timezone('Asia/Shanghai', now()),
    CONSTRAINT uq_eval_run_usage_kb_version UNIQUE (kb_id, eval_version),
    CONSTRAINT ck_eval_run_usage_values CHECK (
        generation_usage_tokens >= 0
        AND generation_estimated_cost_cny >= 0
        AND ragas_input_tokens >= 0
        AND ragas_evaluation_tokens >= 0
        AND ragas_embedding_tokens >= 0
        AND ragas_input_cost_cny >= 0
        AND ragas_evaluation_cost_cny >= 0
        AND ragas_embedding_cost_cny >= 0
    )
);

DO $$
BEGIN
    -- 迁移旧结构中的逐题用量；历史数据只能恢复 run 级总数，没有 RAGAS 明细。
    IF EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_schema = 'public'
          AND table_name = 'kb_eval_result'
          AND column_name = 'usage_tokens'
    ) THEN
        INSERT INTO kb_eval_run_usage (
            kb_id,
            eval_version,
            generation_usage_tokens,
            generation_estimated_cost_cny,
            ragas_input_tokens,
            ragas_evaluation_tokens,
            ragas_embedding_tokens,
            ragas_input_cost_cny,
            ragas_evaluation_cost_cny,
            ragas_embedding_cost_cny
        )
        SELECT
            dataset.kb_id,
            result.eval_version,
            SUM(result.usage_tokens),
            SUM(result.estimated_cost_cny),
            0,
            0,
            0,
            0,
            0,
            0
        FROM kb_eval_result AS result
        JOIN kb_eval_dataset AS dataset
          ON dataset.id = result.dataset_id
        GROUP BY dataset.kb_id, result.eval_version
        ON CONFLICT (kb_id, eval_version) DO NOTHING;
    END IF;
END
$$;

ALTER TABLE kb_eval_result
    DROP CONSTRAINT IF EXISTS ck_eval_result_usage;

ALTER TABLE kb_eval_result
    DROP COLUMN IF EXISTS usage_tokens;

ALTER TABLE kb_eval_result
    DROP COLUMN IF EXISTS estimated_cost_cny;

COMMENT ON TABLE kb_eval_run_usage IS '评估 run 级模型用量与估算成本';
COMMENT ON COLUMN kb_eval_run_usage.id IS '评估 run 用量主键';
COMMENT ON COLUMN kb_eval_run_usage.kb_id IS '所属知识库 ID';
COMMENT ON COLUMN kb_eval_run_usage.eval_version IS '评估版本号';
COMMENT ON COLUMN kb_eval_run_usage.generation_usage_tokens IS '本次评估 RAG 生成管道 Token 总数';
COMMENT ON COLUMN kb_eval_run_usage.generation_estimated_cost_cny IS '本次评估 RAG 生成管道估算成本，单位 CNY';
COMMENT ON COLUMN kb_eval_run_usage.ragas_input_tokens IS 'RAGAS 判定 LLM 输入 Token';
COMMENT ON COLUMN kb_eval_run_usage.ragas_evaluation_tokens IS 'RAGAS 判定 LLM 输出 Token，对应 evaluation 桶';
COMMENT ON COLUMN kb_eval_run_usage.ragas_embedding_tokens IS 'RAGAS 判定 Embedding Token';
COMMENT ON COLUMN kb_eval_run_usage.ragas_input_cost_cny IS 'RAGAS 判定输入估算成本，单位 CNY';
COMMENT ON COLUMN kb_eval_run_usage.ragas_evaluation_cost_cny IS 'RAGAS 判定输出估算成本，单位 CNY';
COMMENT ON COLUMN kb_eval_run_usage.ragas_embedding_cost_cny IS 'RAGAS 判定 Embedding 估算成本，单位 CNY';
COMMENT ON COLUMN kb_eval_run_usage.usage_tokens IS '生成管道与 RAGAS 判定的 Token 总数';
COMMENT ON COLUMN kb_eval_run_usage.estimated_cost_cny IS '生成管道与 RAGAS 判定的估算总成本，单位 CNY';
COMMENT ON COLUMN kb_eval_run_usage.created_at IS 'run 用量写入时间';
