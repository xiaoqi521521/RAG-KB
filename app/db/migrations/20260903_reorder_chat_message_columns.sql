-- 调整用户消息表列顺序：created_at 放在最后。
-- PostgreSQL 不支持直接移动列，因此按目标顺序重建表并保留数据、约束、序列、索引和注释。
DO $$
DECLARE
    current_order text[];
    column_comments jsonb;
    table_comment text;
    table_owner name;
    column_name text;
    column_comment text;
BEGIN
    IF to_regclass('public.kb_chat_message') IS NULL THEN
        RETURN;
    END IF;

    -- 兼容早期数据库：先补齐当前模型已有的消息元数据字段，再执行重排。
    ALTER TABLE kb_chat_message
        ADD COLUMN IF NOT EXISTS answer_mode VARCHAR(30) NOT NULL DEFAULT 'knowledge_base',
        ADD COLUMN IF NOT EXISTS knowledge_base_searched BOOLEAN NOT NULL DEFAULT TRUE;

    SELECT array_agg(c.column_name ORDER BY c.ordinal_position)
      INTO current_order
      FROM information_schema.columns AS c
     WHERE c.table_schema = 'public'
       AND c.table_name = 'kb_chat_message';

    IF current_order = ARRAY[
        'id', 'session_id', 'role', 'content', 'sources', 'token_count',
        'latency_ms', 'feedback', 'kb_ids', 'answer_mode',
        'knowledge_base_searched', 'created_at'
    ] THEN
        RETURN;
    END IF;

    IF EXISTS (
        SELECT 1
          FROM pg_constraint
         WHERE contype = 'f'
           AND confrelid = 'public.kb_chat_message'::regclass
    ) THEN
        RAISE EXCEPTION 'kb_chat_message has foreign-key dependents; column reorder aborted';
    END IF;

    SELECT jsonb_object_agg(attname, col_description(attrelid, attnum))
      INTO column_comments
      FROM pg_attribute
     WHERE attrelid = 'public.kb_chat_message'::regclass
       AND attnum > 0
       AND NOT attisdropped;

    SELECT obj_description('public.kb_chat_message'::regclass, 'pg_class'),
           relowner::regrole::text
      INTO table_comment, table_owner
      FROM pg_class
     WHERE oid = 'public.kb_chat_message'::regclass;

    ALTER SEQUENCE kb_chat_message_id_seq OWNED BY NONE;

    CREATE TABLE kb_chat_message_reordered (
        id                       BIGINT          NOT NULL DEFAULT nextval('kb_chat_message_id_seq'::regclass),
        session_id               VARCHAR(36)     NOT NULL,
        role                     VARCHAR(20)     NOT NULL,
        content                  TEXT            NOT NULL,
        sources                  JSONB,
        token_count              INTEGER         DEFAULT 0,
        latency_ms               INTEGER         DEFAULT 0,
        feedback                 SMALLINT,
        kb_ids                   BIGINT[],
        answer_mode              VARCHAR(30)     NOT NULL DEFAULT 'knowledge_base',
        knowledge_base_searched  BOOLEAN         NOT NULL DEFAULT TRUE,
        created_at               TIMESTAMP       NOT NULL DEFAULT timezone('Asia/Shanghai', now()),
        CONSTRAINT kb_chat_message_reordered_pkey PRIMARY KEY (id)
    );

    INSERT INTO kb_chat_message_reordered (
        id, session_id, role, content, sources, token_count, latency_ms,
        feedback, kb_ids, answer_mode, knowledge_base_searched, created_at
    )
    SELECT
        id, session_id, role, content, sources, token_count, latency_ms,
        feedback, kb_ids, answer_mode, knowledge_base_searched, created_at
      FROM kb_chat_message;

    DROP TABLE kb_chat_message;
    ALTER TABLE kb_chat_message_reordered RENAME TO kb_chat_message;
    ALTER TABLE kb_chat_message RENAME CONSTRAINT kb_chat_message_reordered_pkey
        TO kb_chat_message_pkey;
    ALTER SEQUENCE kb_chat_message_id_seq OWNED BY kb_chat_message.id;
    EXECUTE format('ALTER TABLE kb_chat_message OWNER TO %I', table_owner);

    CREATE INDEX idx_message_session ON kb_chat_message(session_id, created_at);

    IF table_comment IS NOT NULL THEN
        EXECUTE format('COMMENT ON TABLE kb_chat_message IS %L', table_comment);
    END IF;

    FOR column_name, column_comment IN
        SELECT key, value #>> '{}'
          FROM jsonb_each(column_comments)
    LOOP
        IF column_comment IS NOT NULL THEN
            EXECUTE format(
                'COMMENT ON COLUMN kb_chat_message.%I IS %L',
                column_name,
                column_comment
            );
        END IF;
    END LOOP;
END;
$$;
