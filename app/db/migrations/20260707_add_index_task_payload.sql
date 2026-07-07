ALTER TABLE kb_index_task
    ADD COLUMN IF NOT EXISTS payload JSONB;

COMMENT ON COLUMN kb_index_task.payload IS '索引任务扩展载荷，例如文档替换时暂存待发布文件元数据';
