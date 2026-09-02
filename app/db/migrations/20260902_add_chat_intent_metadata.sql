ALTER TABLE kb_chat_message
    ADD COLUMN IF NOT EXISTS answer_mode VARCHAR(30) NOT NULL DEFAULT 'knowledge_base',
    ADD COLUMN IF NOT EXISTS knowledge_base_searched BOOLEAN NOT NULL DEFAULT TRUE;
