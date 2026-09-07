-- 调整评估结果列顺序：actual_answer 位于 eval_version 与 hit 之间，eval_at 放在最后。
-- PostgreSQL 不支持直接移动列，因此按目标顺序重建表并保留数据、约束、序列和注释。
DO $$
DECLARE
    current_order text[];
    column_comments jsonb;
    table_comment text;
    table_owner name;
    eval_version_type text;
    column_name text;
    column_comment text;
BEGIN
    IF to_regclass('public.kb_eval_result') IS NULL THEN
        RETURN;
    END IF;

    SELECT array_agg(c.column_name ORDER BY c.ordinal_position)
      INTO current_order
      FROM information_schema.columns AS c
     WHERE c.table_schema = 'public'
       AND c.table_name = 'kb_eval_result';

    SELECT c.data_type
      INTO eval_version_type
      FROM information_schema.columns AS c
     WHERE c.table_schema = 'public'
       AND c.table_name = 'kb_eval_result'
       AND c.column_name = 'eval_version';

    IF current_order = ARRAY[
        'id', 'dataset_id', 'eval_version', 'actual_answer', 'hit', 'rank',
        'faithfulness', 'answer_relevancy', 'context_recall',
        'context_precision', 'status', 'error_type', 'eval_at'
    ] AND eval_version_type = 'integer' THEN
        RETURN;
    END IF;

    IF EXISTS (
        SELECT 1
          FROM pg_constraint
         WHERE contype = 'f'
           AND confrelid = 'public.kb_eval_result'::regclass
    ) THEN
        RAISE EXCEPTION 'kb_eval_result has foreign-key dependents; column reorder aborted';
    END IF;

    IF EXISTS (
        SELECT 1
          FROM kb_eval_result
         WHERE trim(eval_version::text) !~ '^[vV]?[0-9]+([_-].*)?$'
    ) THEN
        RAISE EXCEPTION 'kb_eval_result.eval_version contains values that cannot be converted to int4';
    END IF;

    SELECT jsonb_object_agg(attname, col_description(attrelid, attnum))
      INTO column_comments
      FROM pg_attribute
     WHERE attrelid = 'public.kb_eval_result'::regclass
       AND attnum > 0
       AND NOT attisdropped;
    SELECT obj_description('public.kb_eval_result'::regclass, 'pg_class'),
           relowner::regrole::text
      INTO table_comment, table_owner
      FROM pg_class
     WHERE oid = 'public.kb_eval_result'::regclass;

    ALTER SEQUENCE kb_eval_result_id_seq OWNED BY NONE;

    CREATE TABLE kb_eval_result_reordered (
        id                  BIGINT          NOT NULL DEFAULT nextval('kb_eval_result_id_seq'::regclass),
        dataset_id          BIGINT          NOT NULL,
        eval_version        INTEGER         NOT NULL,
        actual_answer       TEXT,
        hit                 BOOLEAN,
        rank                INTEGER,
        faithfulness        FLOAT,
        answer_relevancy    FLOAT,
        context_recall     FLOAT,
        context_precision  FLOAT,
        status              VARCHAR(20)     NOT NULL,
        error_type          VARCHAR(100),
        eval_at             TIMESTAMP       NOT NULL DEFAULT timezone('Asia/Shanghai', now()),
        CONSTRAINT kb_eval_result_reordered_pkey PRIMARY KEY (id),
        CONSTRAINT kb_eval_result_reordered_dataset_version UNIQUE (dataset_id, eval_version),
        CONSTRAINT kb_eval_result_reordered_status CHECK (
            status IN ('SUCCESS', 'PARTIAL', 'FAILED')
        ),
        CONSTRAINT kb_eval_result_reordered_rank CHECK (
            rank IS NULL OR rank BETWEEN 1 AND 5
        ),
        CONSTRAINT kb_eval_result_reordered_scores CHECK (
            (faithfulness IS NULL OR faithfulness BETWEEN 0.0 AND 1.0)
            AND (answer_relevancy IS NULL OR answer_relevancy BETWEEN 0.0 AND 1.0)
            AND (context_recall IS NULL OR context_recall BETWEEN 0.0 AND 1.0)
            AND (context_precision IS NULL OR context_precision BETWEEN 0.0 AND 1.0)
        )
    );

    INSERT INTO kb_eval_result_reordered (
        id, dataset_id, eval_version, actual_answer, hit, rank,
        faithfulness, answer_relevancy, context_recall, context_precision,
        status, error_type, eval_at
    )
    SELECT
        id, dataset_id,
        substring(trim(eval_version::text) FROM '^[vV]?([0-9]+)')::INTEGER,
        actual_answer, hit, rank,
        faithfulness, answer_relevancy, context_recall, context_precision,
        status, error_type, eval_at
      FROM kb_eval_result;

    DROP TABLE kb_eval_result;
    ALTER TABLE kb_eval_result_reordered RENAME TO kb_eval_result;
    ALTER TABLE kb_eval_result RENAME CONSTRAINT kb_eval_result_reordered_pkey
        TO kb_eval_result_pkey;
    ALTER TABLE kb_eval_result RENAME CONSTRAINT kb_eval_result_reordered_dataset_version
        TO uq_eval_result_dataset_version;
    ALTER TABLE kb_eval_result RENAME CONSTRAINT kb_eval_result_reordered_status
        TO ck_eval_result_status;
    ALTER TABLE kb_eval_result RENAME CONSTRAINT kb_eval_result_reordered_rank
        TO ck_eval_result_rank;
    ALTER TABLE kb_eval_result RENAME CONSTRAINT kb_eval_result_reordered_scores
        TO ck_eval_result_scores;
    ALTER SEQUENCE kb_eval_result_id_seq OWNED BY kb_eval_result.id;
    EXECUTE format('ALTER TABLE kb_eval_result OWNER TO %I', table_owner);

    IF table_comment IS NOT NULL THEN
        EXECUTE format('COMMENT ON TABLE kb_eval_result IS %L', table_comment);
    END IF;

    FOR column_name, column_comment IN
        SELECT key, value #>> '{}'
          FROM jsonb_each(column_comments)
    LOOP
        IF column_comment IS NOT NULL THEN
            EXECUTE format(
                'COMMENT ON COLUMN kb_eval_result.%I IS %L',
                column_name,
                column_comment
            );
        END IF;
    END LOOP;
END;
$$;
