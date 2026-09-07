-- 将评估结果版本从旧的 vN_* 字符串标识迁移为与 document.version 一致的 int4。
DO $$
DECLARE
    data_type text;
BEGIN
    IF to_regclass('public.kb_eval_result') IS NULL THEN
        RETURN;
    END IF;

    SELECT c.data_type
      INTO data_type
      FROM information_schema.columns AS c
     WHERE c.table_schema = 'public'
       AND c.table_name = 'kb_eval_result'
       AND c.column_name = 'eval_version';

    IF data_type = 'integer' THEN
        RETURN;
    END IF;
    IF data_type IS DISTINCT FROM 'character varying' THEN
        RAISE EXCEPTION 'unsupported kb_eval_result.eval_version type: %', data_type;
    END IF;

    IF EXISTS (
        SELECT 1
          FROM kb_eval_result
         WHERE trim(eval_version) !~ '^[vV]?[0-9]+([_-].*)?$'
    ) THEN
        RAISE EXCEPTION 'kb_eval_result.eval_version contains values that cannot be converted to int4';
    END IF;

    ALTER TABLE kb_eval_result
        ALTER COLUMN eval_version TYPE INTEGER
        USING substring(trim(eval_version) FROM '^[vV]?([0-9]+)')::INTEGER;

    COMMENT ON COLUMN kb_eval_result.eval_version IS '评估版本号，对应文档 version';
END;
$$;
