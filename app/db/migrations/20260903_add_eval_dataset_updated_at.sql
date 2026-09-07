-- 为评估数据集增加更新时间，并确保任意 UPDATE 都会自动刷新该字段。
ALTER TABLE kb_eval_dataset
    ADD COLUMN IF NOT EXISTS updated_at TIMESTAMP;

UPDATE kb_eval_dataset
SET updated_at = COALESCE(updated_at, created_at, timezone('Asia/Shanghai', now()));

ALTER TABLE kb_eval_dataset
    ALTER COLUMN updated_at SET DEFAULT timezone('Asia/Shanghai', now()),
    ALTER COLUMN updated_at SET NOT NULL;

CREATE OR REPLACE FUNCTION update_eval_dataset_updated_at()
RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at := timezone('Asia/Shanghai', now());
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trigger_eval_dataset_updated_at ON kb_eval_dataset;
CREATE TRIGGER trigger_eval_dataset_updated_at
    BEFORE UPDATE ON kb_eval_dataset
    FOR EACH ROW
    EXECUTE FUNCTION update_eval_dataset_updated_at();

COMMENT ON COLUMN kb_eval_dataset.updated_at IS '最后更新时间，编辑或归档后自动刷新';
