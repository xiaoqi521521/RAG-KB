-- 调整评估数据集列顺序：created_at 位于 source_feedback_id 与 updated_at 之间。
-- PostgreSQL 不支持直接移动列，因此按目标顺序重建表并保留数据、索引、注释和触发器。
DO $$
DECLARE
    current_order text[];
    column_comments jsonb;
    table_comment text;
    table_owner name;
    column_name text;
    column_comment text;
BEGIN
    SELECT array_agg(c.column_name ORDER BY c.ordinal_position)
      INTO current_order
      FROM information_schema.columns AS c
     WHERE c.table_schema = 'public'
       AND c.table_name = 'kb_eval_dataset';

    IF current_order = ARRAY[
        'id', 'kb_id', 'question', 'expected_answer', 'expected_chunk_ids',
        'created_by', 'status', 'review_reason', 'source_feedback_id',
        'created_at', 'updated_at'
    ] THEN
        RETURN;
    END IF;

    IF EXISTS (
        SELECT 1
          FROM pg_constraint
         WHERE contype = 'f'
           AND confrelid = 'public.kb_eval_dataset'::regclass
    ) THEN
        RAISE EXCEPTION 'kb_eval_dataset has foreign-key dependents; column reorder aborted';
    END IF;

    SELECT jsonb_object_agg(attname, col_description(attrelid, attnum))
      INTO column_comments
      FROM pg_attribute
     WHERE attrelid = 'public.kb_eval_dataset'::regclass
       AND attnum > 0
       AND NOT attisdropped;
    SELECT obj_description('public.kb_eval_dataset'::regclass, 'pg_class'),
           relowner::regrole::text
      INTO table_comment, table_owner
      FROM pg_class
     WHERE oid = 'public.kb_eval_dataset'::regclass;

    ALTER SEQUENCE kb_eval_dataset_id_seq OWNED BY NONE;

    CREATE TABLE kb_eval_dataset_reordered (
        id                  BIGINT          NOT NULL DEFAULT nextval('kb_eval_dataset_id_seq'::regclass),
        kb_id               BIGINT          NOT NULL,
        question            TEXT            NOT NULL,
        expected_answer     TEXT,
        expected_chunk_ids  BIGINT[],
        created_by          BIGINT          NOT NULL,
        status              VARCHAR(20)     NOT NULL DEFAULT 'ACTIVE',
        review_reason       VARCHAR(50),
        source_feedback_id  BIGINT,
        created_at          TIMESTAMP       NOT NULL DEFAULT timezone('Asia/Shanghai', now()),
        updated_at          TIMESTAMP       NOT NULL DEFAULT timezone('Asia/Shanghai', now()),
        CONSTRAINT kb_eval_dataset_reordered_pkey PRIMARY KEY (id),
        CONSTRAINT ck_eval_dataset_status CHECK (
            status IN ('CANDIDATE', 'ACTIVE', 'NEEDS_REVIEW', 'ARCHIVED')
        )
    );

    INSERT INTO kb_eval_dataset_reordered (
        id, kb_id, question, expected_answer, expected_chunk_ids, created_by,
        status, review_reason, source_feedback_id, created_at, updated_at
    )
    SELECT
        id, kb_id, question, expected_answer, expected_chunk_ids, created_by,
        status, review_reason, source_feedback_id, created_at,
        COALESCE(updated_at, created_at, timezone('Asia/Shanghai', now()))
      FROM kb_eval_dataset;

    DROP TABLE kb_eval_dataset;
    ALTER TABLE kb_eval_dataset_reordered RENAME TO kb_eval_dataset;
    ALTER TABLE kb_eval_dataset RENAME CONSTRAINT kb_eval_dataset_reordered_pkey
        TO kb_eval_dataset_pkey;
    ALTER SEQUENCE kb_eval_dataset_id_seq OWNED BY kb_eval_dataset.id;
    EXECUTE format('ALTER TABLE kb_eval_dataset OWNER TO %I', table_owner);

    CREATE INDEX idx_eval_dataset_kb_status ON kb_eval_dataset(kb_id, status);
    CREATE UNIQUE INDEX uq_eval_dataset_source_feedback
        ON kb_eval_dataset(source_feedback_id)
        WHERE source_feedback_id IS NOT NULL;

    CREATE OR REPLACE FUNCTION update_eval_dataset_updated_at()
    RETURNS TRIGGER AS $fn$
    BEGIN
        NEW.updated_at := timezone('Asia/Shanghai', now());
        RETURN NEW;
    END;
    $fn$ LANGUAGE plpgsql;

    CREATE TRIGGER trigger_eval_dataset_updated_at
        BEFORE UPDATE ON kb_eval_dataset
        FOR EACH ROW
        EXECUTE FUNCTION update_eval_dataset_updated_at();

    IF table_comment IS NOT NULL THEN
        EXECUTE format('COMMENT ON TABLE kb_eval_dataset IS %L', table_comment);
    END IF;

    FOR column_name, column_comment IN
        SELECT key, value #>> '{}'
          FROM jsonb_each(column_comments)
    LOOP
        IF column_comment IS NOT NULL THEN
            EXECUTE format(
                'COMMENT ON COLUMN kb_eval_dataset.%I IS %L',
                column_name,
                column_comment
            );
        END IF;
    END LOOP;
END;
$$;
