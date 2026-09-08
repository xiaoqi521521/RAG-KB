-- ================================================================
-- rag-kb 数据库初始化脚本
-- 执行前确保已创建数据库：CREATE DATABASE ragkb;
-- ================================================================

-- 启用 PGVector 扩展（必须，向量存储依赖）
CREATE EXTENSION IF NOT EXISTS vector;

-- ================================================================
-- 1. 知识库表
-- ================================================================
CREATE TABLE kb_knowledge_base (
    id              BIGSERIAL PRIMARY KEY,
    name            VARCHAR(100)    NOT NULL,
    description     TEXT,
    department_id   VARCHAR(50)     NOT NULL,           -- 归属部门
    is_public       BOOLEAN         NOT NULL DEFAULT FALSE, -- 是否对所有人开放
    created_by      BIGINT          NOT NULL,           -- 创建者 userId
    created_at      TIMESTAMP       NOT NULL DEFAULT timezone('Asia/Shanghai', now()),
    updated_at      TIMESTAMP       NOT NULL DEFAULT timezone('Asia/Shanghai', now()),
    is_deleted      BOOLEAN         NOT NULL DEFAULT FALSE
);

COMMENT ON TABLE kb_knowledge_base IS '知识库，一个部门可以有多个知识库';
CREATE INDEX idx_kb_department ON kb_knowledge_base(department_id) WHERE is_deleted = FALSE;

-- ================================================================
-- 2. 知识库权限表
-- ================================================================
CREATE TABLE kb_permission (
    id              BIGSERIAL PRIMARY KEY,
    kb_id           BIGINT          NOT NULL,
    subject_type    VARCHAR(20)     NOT NULL,           -- DEPARTMENT / USER
    subject_id      VARCHAR(50)     NOT NULL,           -- 部门ID 或 userId
    permission      VARCHAR(20)     NOT NULL,           -- READ / WRITE / ADMIN
    granted_by      BIGINT          NOT NULL,
    granted_at      TIMESTAMP       NOT NULL DEFAULT timezone('Asia/Shanghai', now()),
    UNIQUE (kb_id, subject_type, subject_id)
);

CREATE INDEX idx_permission_subject ON kb_permission(subject_type, subject_id);

-- ================================================================
-- 3. 文档表
-- ================================================================
CREATE TABLE kb_document (
    id              BIGSERIAL PRIMARY KEY,
    kb_id           BIGINT          NOT NULL,
    file_name       VARCHAR(255)    NOT NULL,
    file_type       VARCHAR(20)     NOT NULL,           -- PDF / DOCX / MD / TXT
    file_size       BIGINT          NOT NULL,           -- 字节数
    minio_path      VARCHAR(500)    NOT NULL,           -- MinIO 中的对象路径
    status          VARCHAR(20)     NOT NULL DEFAULT 'PENDING',
                                                        -- PENDING=待处理 / PROCESSING=处理中 / DONE=索引完成 / FAILED=索引失败
    error_msg       TEXT,                              -- 失败原因
    chunk_count     INT             DEFAULT 0,          -- 索引后的分块数量
    token_count     INT             DEFAULT 0,          -- 向量化消耗的 Token 数
    version         INT             NOT NULL DEFAULT 1, -- 文档版本号，更新时递增
    uploaded_by     BIGINT          NOT NULL,
    uploaded_at     TIMESTAMP       NOT NULL DEFAULT timezone('Asia/Shanghai', now()),
    indexed_at      TIMESTAMP,                         -- 最近一次索引完成时间
    is_deleted      BOOLEAN         NOT NULL DEFAULT FALSE
);

COMMENT ON TABLE kb_document IS '上传到知识库的文档，一个文档对应多个分块';
COMMENT ON COLUMN kb_document.version IS '每次重建索引版本号加1，旧版本分块通过版本号识别并删除';

CREATE INDEX idx_doc_kb_id ON kb_document(kb_id) WHERE is_deleted = FALSE;
CREATE INDEX idx_doc_status ON kb_document(status) WHERE is_deleted = FALSE;

-- ================================================================
-- 4. 文档分块表（核心表，含向量字段）
-- ================================================================
CREATE TABLE kb_doc_chunk (
    id              BIGSERIAL PRIMARY KEY,
    doc_id          BIGINT          NOT NULL,
    kb_id           BIGINT          NOT NULL,           -- 冗余存储，检索时避免 JOIN
    chunk_index     INT             NOT NULL,           -- 在文档中的顺序（0-based）
    content         TEXT            NOT NULL,           -- 分块原文
    content_tsv     TSVECTOR,                          -- 全文检索索引（自动维护）
    embedding       VECTOR(1024)    NOT NULL,           -- 向量（text-embedding-v3 是 1024 维）
    page_num        INT,                               -- 来自文档第几页（PDF 专用）
    section_title   VARCHAR(500),                      -- 所在章节标题（如果能识别）
    token_count     INT             NOT NULL DEFAULT 0, -- 该块的 Token 估算数
    doc_version     INT             NOT NULL,           -- 对应的文档版本号
    created_at      TIMESTAMP       NOT NULL DEFAULT timezone('Asia/Shanghai', now())
);

COMMENT ON TABLE kb_doc_chunk IS '文档分块表，每条记录是一个可检索的最小单元';
COMMENT ON COLUMN kb_doc_chunk.content_tsv IS '全文检索向量，由触发器自动更新';
COMMENT ON COLUMN kb_doc_chunk.doc_version IS '冗余版本号，重建索引后删除旧版本时使用';

-- 向量检索索引（HNSW，适合高并发检索）
-- m=16: 每个节点的最大连接数，越大越准但更占内存
-- ef_construction=128: 构建索引时的搜索宽度，越大越准但建索引更慢
CREATE INDEX idx_chunk_embedding ON kb_doc_chunk
    USING hnsw (embedding vector_cosine_ops)
    WITH (m = 16, ef_construction = 128);

-- 全文检索索引
CREATE INDEX idx_chunk_content_tsv ON kb_doc_chunk USING GIN (content_tsv);

-- 按 kb_id 过滤的索引（多租户场景必须有）
CREATE INDEX idx_chunk_kb_id ON kb_doc_chunk(kb_id);
CREATE INDEX idx_chunk_doc_id ON kb_doc_chunk(doc_id);

-- 触发器：自动维护全文检索文档表示（tsvector）
-- 简单版：用默认英文分词（中文效果一般，但不需要额外扩展）
-- 注意：中文全文检索效果不佳，主要靠向量检索；全文检索作为补充用于精确词搜索
CREATE OR REPLACE FUNCTION update_chunk_tsv()
RETURNS TRIGGER AS $$
BEGIN
    NEW.content_tsv := to_tsvector('simple', NEW.content);
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER trigger_chunk_tsv
    BEFORE INSERT OR UPDATE OF content
    ON kb_doc_chunk
    FOR EACH ROW
    EXECUTE FUNCTION update_chunk_tsv();

-- ================================================================
-- 5. 索引任务表（异步索引状态管理）
-- ================================================================
CREATE TABLE kb_index_task (
    id              BIGSERIAL PRIMARY KEY,
    doc_id          BIGINT          NOT NULL,
    task_type       VARCHAR(20)     NOT NULL DEFAULT 'INDEX',  -- INDEX / REINDEX
    status          VARCHAR(20)     NOT NULL DEFAULT 'PENDING', -- PENDING=待执行 / RUNNING=执行中 / DONE=执行完成 / FAILED=执行失败
    retry_count     INT             NOT NULL DEFAULT 0,
    max_retry       INT             NOT NULL DEFAULT 3,
    payload         JSONB,
    error_msg       TEXT,
    created_at      TIMESTAMP       NOT NULL DEFAULT timezone('Asia/Shanghai', now()),
    started_at      TIMESTAMP,
    finished_at     TIMESTAMP
);

COMMENT ON COLUMN kb_index_task.payload IS '索引任务扩展载荷，例如文档替换时暂存待发布文件元数据';

CREATE INDEX idx_task_status ON kb_index_task(status, created_at);
CREATE INDEX idx_task_doc_id ON kb_index_task(doc_id);

-- ================================================================
-- 6. 对话会话表
-- ================================================================
CREATE TABLE kb_chat_session (
    id              VARCHAR(36)     PRIMARY KEY,        -- UUID
    user_id         BIGINT          NOT NULL,
    kb_ids          TEXT            NOT NULL,           -- JSON 数组，查询的知识库列表
    title           VARCHAR(200),                      -- 会话标题（取第一条消息）
    message_count   INT             NOT NULL DEFAULT 0,
    created_at      TIMESTAMP       NOT NULL DEFAULT timezone('Asia/Shanghai', now()),
    last_active_at  TIMESTAMP       NOT NULL DEFAULT timezone('Asia/Shanghai', now()),
    is_deleted      BOOLEAN         NOT NULL DEFAULT FALSE
);

CREATE INDEX idx_session_user ON kb_chat_session(user_id, last_active_at DESC)
    WHERE is_deleted = FALSE;

-- ================================================================
-- 7. 对话消息表
-- ================================================================
CREATE TABLE kb_chat_message (
    id              BIGSERIAL PRIMARY KEY,
    session_id      VARCHAR(36)     NOT NULL,
    role            VARCHAR(20)     NOT NULL,           -- USER / ASSISTANT
    content         TEXT            NOT NULL,
    sources         JSONB,                             -- 引用来源列表（仅 ASSISTANT 消息有）
    token_count     INT             DEFAULT 0,          -- 消耗的 Token 数
    latency_ms      INT             DEFAULT 0,          -- 生成耗时（毫秒）
    feedback        SMALLINT,                          -- 用户反馈：1=好 -1=差 NULL=未反馈
    kb_ids          BIGINT[],                          -- 本轮助手回答使用的完整知识库范围
    answer_mode     VARCHAR(30) NOT NULL DEFAULT 'knowledge_base',
    knowledge_base_searched BOOLEAN NOT NULL DEFAULT TRUE,
    created_at      TIMESTAMP       NOT NULL DEFAULT timezone('Asia/Shanghai', now())
);

COMMENT ON COLUMN kb_chat_message.sources IS
    'JSON格式：[{"docId":1,"docName":"手册.pdf","chunkId":100,"pageNum":5,"excerpt":"...","score":0.92}]';

CREATE INDEX idx_message_session ON kb_chat_message(session_id, created_at);

-- ================================================================
-- 8. 用户反馈表（点赞/点踩，用于效果评估）
-- ================================================================
CREATE TABLE kb_answer_feedback (
    id              BIGSERIAL PRIMARY KEY,
    message_id      BIGINT          NOT NULL,
    user_id         BIGINT          NOT NULL,
    feedback        SMALLINT        NOT NULL,           -- 1=有用 -1=待改进 0=已取消
    comment         TEXT,                              -- 可选的文字反馈
    created_at      TIMESTAMP       NOT NULL DEFAULT timezone('Asia/Shanghai', now()),
    updated_at      TIMESTAMP       NOT NULL DEFAULT timezone('Asia/Shanghai', now()),
    CONSTRAINT uq_answer_feedback_message_user UNIQUE (message_id, user_id),
    CONSTRAINT ck_answer_feedback_value CHECK (feedback IN (-1, 0, 1))
);

-- 反馈重复提交、取消等更新均自动刷新更新时间。
CREATE OR REPLACE FUNCTION update_answer_feedback_updated_at()
RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at := timezone('Asia/Shanghai', now());
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER trigger_answer_feedback_updated_at
    BEFORE UPDATE ON kb_answer_feedback
    FOR EACH ROW
    EXECUTE FUNCTION update_answer_feedback_updated_at();

-- ================================================================
-- 9. RAG 评估数据集表
-- ================================================================
CREATE TABLE kb_eval_dataset (
    id              BIGSERIAL PRIMARY KEY,
    kb_id           BIGINT          NOT NULL,
    question        TEXT            NOT NULL,
    expected_answer TEXT,
    expected_chunk_ids  BIGINT[],                      -- 期望召回的 chunk ID
    created_by          BIGINT          NOT NULL,
    status              VARCHAR(20)     NOT NULL DEFAULT 'ACTIVE',
    review_reason       VARCHAR(50),
    source_feedback_id  BIGINT UNIQUE,
    created_at          TIMESTAMP       NOT NULL DEFAULT timezone('Asia/Shanghai', now()),
    updated_at          TIMESTAMP       NOT NULL DEFAULT timezone('Asia/Shanghai', now()),
    CONSTRAINT ck_eval_dataset_status
        CHECK (status IN ('CANDIDATE', 'ACTIVE', 'NEEDS_REVIEW', 'ARCHIVED'))
);

CREATE INDEX idx_eval_dataset_kb_status ON kb_eval_dataset(kb_id, status);

-- 评估数据集的编辑、归档等更新均自动刷新更新时间。
CREATE OR REPLACE FUNCTION update_eval_dataset_updated_at()
RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at := timezone('Asia/Shanghai', now());
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER trigger_eval_dataset_updated_at
    BEFORE UPDATE ON kb_eval_dataset
    FOR EACH ROW
    EXECUTE FUNCTION update_eval_dataset_updated_at();

CREATE TABLE kb_eval_result (
    id              BIGSERIAL PRIMARY KEY,
    dataset_id      BIGINT          NOT NULL,
    eval_version    INTEGER         NOT NULL,           -- 评估版本号（对应文档 version）
    actual_answer   TEXT,
    hit             BOOLEAN,                            -- NULL 表示不参与检索指标
    rank            INT,                               -- 命中 chunk 的排名（用于 MRR 计算）
    faithfulness    FLOAT,                             -- RAGAS Faithfulness 分数
    answer_relevancy FLOAT,                            -- RAGAS Answer Relevancy 分数
    context_recall  FLOAT,
    context_precision FLOAT,
    status              VARCHAR(20)     NOT NULL,
    error_type          VARCHAR(100),
    duration_ms         INTEGER,                           -- 整轮评估执行耗时；历史数据可为空
    eval_at         TIMESTAMP       NOT NULL DEFAULT timezone('Asia/Shanghai', now()),
    CONSTRAINT uq_eval_result_dataset_version UNIQUE (dataset_id, eval_version),
    CONSTRAINT ck_eval_result_status CHECK (status IN ('SUCCESS', 'PARTIAL', 'FAILED')),
    CONSTRAINT ck_eval_result_rank CHECK (rank IS NULL OR rank BETWEEN 1 AND 5),
    CONSTRAINT ck_eval_result_scores CHECK (
        (faithfulness IS NULL OR faithfulness BETWEEN 0.0 AND 1.0)
        AND (answer_relevancy IS NULL OR answer_relevancy BETWEEN 0.0 AND 1.0)
        AND (context_recall IS NULL OR context_recall BETWEEN 0.0 AND 1.0)
        AND (context_precision IS NULL OR context_precision BETWEEN 0.0 AND 1.0)
    ),
    CONSTRAINT ck_eval_result_duration CHECK (duration_ms IS NULL OR duration_ms >= 0)
);

CREATE TABLE kb_eval_run_usage (
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

-- ================================================================
-- 10. 字段注释
-- ================================================================
COMMENT ON COLUMN kb_knowledge_base.id IS '知识库主键';
COMMENT ON COLUMN kb_knowledge_base.name IS '知识库名称';
COMMENT ON COLUMN kb_knowledge_base.description IS '知识库描述';
COMMENT ON COLUMN kb_knowledge_base.department_id IS '归属部门标识';
COMMENT ON COLUMN kb_knowledge_base.is_public IS '是否对所有已认证用户公开只读';
COMMENT ON COLUMN kb_knowledge_base.created_by IS '创建用户 ID';
COMMENT ON COLUMN kb_knowledge_base.created_at IS '创建时间';
COMMENT ON COLUMN kb_knowledge_base.updated_at IS '最后更新时间';
COMMENT ON COLUMN kb_knowledge_base.is_deleted IS '是否逻辑删除';

COMMENT ON COLUMN kb_permission.id IS '权限记录主键';
COMMENT ON COLUMN kb_permission.kb_id IS '知识库 ID';
COMMENT ON COLUMN kb_permission.subject_type IS '授权主体类型：DEPARTMENT 或 USER';
COMMENT ON COLUMN kb_permission.subject_id IS '授权主体标识：部门 ID 或用户 ID';
COMMENT ON COLUMN kb_permission.permission IS '权限级别：READ、WRITE 或 ADMIN';
COMMENT ON COLUMN kb_permission.granted_by IS '授权操作用户 ID';
COMMENT ON COLUMN kb_permission.granted_at IS '授权时间';

COMMENT ON COLUMN kb_document.id IS '文档主键';
COMMENT ON COLUMN kb_document.kb_id IS '所属知识库 ID';
COMMENT ON COLUMN kb_document.file_name IS '原始文件名';
COMMENT ON COLUMN kb_document.file_type IS '文件类型：PDF、DOCX、MD 或 TXT';
COMMENT ON COLUMN kb_document.file_size IS '文件大小，单位为字节';
COMMENT ON COLUMN kb_document.minio_path IS 'MinIO 对象路径';
COMMENT ON COLUMN kb_document.status IS '索引状态：PENDING=待处理，PROCESSING=处理中，DONE=索引完成，FAILED=索引失败';
COMMENT ON COLUMN kb_document.error_msg IS '最近一次索引失败原因';
COMMENT ON COLUMN kb_document.chunk_count IS '当前版本分块数量';
COMMENT ON COLUMN kb_document.token_count IS '当前版本向量化消耗的 Token 数';
COMMENT ON COLUMN kb_document.version IS '文档版本号，重建索引时递增';
COMMENT ON COLUMN kb_document.uploaded_by IS '上传用户 ID';
COMMENT ON COLUMN kb_document.uploaded_at IS '上传时间';
COMMENT ON COLUMN kb_document.indexed_at IS '最近一次索引完成时间';
COMMENT ON COLUMN kb_document.is_deleted IS '是否逻辑删除';

COMMENT ON COLUMN kb_doc_chunk.id IS '文档分块主键';
COMMENT ON COLUMN kb_doc_chunk.doc_id IS '所属文档 ID';
COMMENT ON COLUMN kb_doc_chunk.kb_id IS '所属知识库 ID，冗余保存以便检索过滤';
COMMENT ON COLUMN kb_doc_chunk.chunk_index IS '分块在文档内的零基顺序';
COMMENT ON COLUMN kb_doc_chunk.content IS '分块原文内容';
COMMENT ON COLUMN kb_doc_chunk.content_tsv IS '全文检索向量，由触发器自动更新';
COMMENT ON COLUMN kb_doc_chunk.embedding IS 'text-embedding-v3 的 1024 维向量';
COMMENT ON COLUMN kb_doc_chunk.page_num IS '原文页码，PDF 文档适用';
COMMENT ON COLUMN kb_doc_chunk.section_title IS '原文所在章节标题';
COMMENT ON COLUMN kb_doc_chunk.token_count IS '分块 Token 估算数';
COMMENT ON COLUMN kb_doc_chunk.doc_version IS '对应文档版本号，用于清理旧分块';
COMMENT ON COLUMN kb_doc_chunk.created_at IS '分块创建时间';

COMMENT ON COLUMN kb_index_task.id IS '索引任务主键';
COMMENT ON COLUMN kb_index_task.doc_id IS '待处理文档 ID';
COMMENT ON COLUMN kb_index_task.task_type IS '任务类型：INDEX 或 REINDEX';
COMMENT ON COLUMN kb_index_task.status IS '任务状态：PENDING=待执行，RUNNING=执行中，DONE=执行完成，FAILED=执行失败';
COMMENT ON COLUMN kb_index_task.retry_count IS '已重试次数';
COMMENT ON COLUMN kb_index_task.max_retry IS '最大重试次数';
COMMENT ON COLUMN kb_index_task.payload IS '索引任务扩展载荷';
COMMENT ON COLUMN kb_index_task.error_msg IS '最近一次任务失败原因';
COMMENT ON COLUMN kb_index_task.created_at IS '任务创建时间';
COMMENT ON COLUMN kb_index_task.started_at IS '任务开始时间';
COMMENT ON COLUMN kb_index_task.finished_at IS '任务完成或失败时间';

COMMENT ON COLUMN kb_chat_session.id IS '会话 UUID';
COMMENT ON COLUMN kb_chat_session.user_id IS '会话所属用户 ID';
COMMENT ON COLUMN kb_chat_session.kb_ids IS '会话查询知识库 ID 列表，JSON 编码';
COMMENT ON COLUMN kb_chat_session.title IS '会话标题';
COMMENT ON COLUMN kb_chat_session.message_count IS '会话消息数量';
COMMENT ON COLUMN kb_chat_session.created_at IS '会话创建时间';
COMMENT ON COLUMN kb_chat_session.last_active_at IS '最后活跃时间';
COMMENT ON COLUMN kb_chat_session.is_deleted IS '是否逻辑删除';

COMMENT ON COLUMN kb_chat_message.id IS '消息主键';
COMMENT ON COLUMN kb_chat_message.session_id IS '所属会话 UUID';
COMMENT ON COLUMN kb_chat_message.role IS '消息角色：USER 或 ASSISTANT';
COMMENT ON COLUMN kb_chat_message.content IS '消息内容';
COMMENT ON COLUMN kb_chat_message.sources IS '助手回答引用来源列表，JSON 格式';
COMMENT ON COLUMN kb_chat_message.token_count IS '本条消息消耗的 Token 数';
COMMENT ON COLUMN kb_chat_message.latency_ms IS '生成耗时，单位为毫秒';
COMMENT ON COLUMN kb_chat_message.feedback IS '消息反馈状态，兼容保留字段';
COMMENT ON COLUMN kb_chat_message.kb_ids IS '本轮回答实际使用的知识库 ID 范围';
COMMENT ON COLUMN kb_chat_message.answer_mode IS '回答模式：knowledge_base、session_meta、general_chat 或 uncertain';
COMMENT ON COLUMN kb_chat_message.knowledge_base_searched IS '本轮是否执行知识库检索';
COMMENT ON COLUMN kb_chat_message.created_at IS '消息创建时间';

COMMENT ON COLUMN kb_answer_feedback.id IS '回答反馈主键';
COMMENT ON COLUMN kb_answer_feedback.message_id IS '被反馈的助手消息 ID';
COMMENT ON COLUMN kb_answer_feedback.user_id IS '提交反馈的用户 ID';
COMMENT ON COLUMN kb_answer_feedback.feedback IS '反馈值：1 有用、-1 待改进、0 已取消';
COMMENT ON COLUMN kb_answer_feedback.comment IS '可选文字反馈';
COMMENT ON COLUMN kb_answer_feedback.created_at IS '反馈创建时间';
COMMENT ON COLUMN kb_answer_feedback.updated_at IS '最后更新时间，反馈更新后自动刷新';

COMMENT ON COLUMN kb_eval_dataset.id IS '评估问题主键';
COMMENT ON COLUMN kb_eval_dataset.kb_id IS '所属知识库 ID';
COMMENT ON COLUMN kb_eval_dataset.question IS '待评估问题';
COMMENT ON COLUMN kb_eval_dataset.expected_answer IS '期望回答';
COMMENT ON COLUMN kb_eval_dataset.expected_chunk_ids IS '期望召回的分块 ID 列表';
COMMENT ON COLUMN kb_eval_dataset.status IS '数据集状态：CANDIDATE=候选待审核，ACTIVE=有效并纳入评估，NEEDS_REVIEW=关联内容变更后待复核，ARCHIVED=已归档且不纳入默认评估';
COMMENT ON COLUMN kb_eval_dataset.review_reason IS '需要审核的原因';
COMMENT ON COLUMN kb_eval_dataset.source_feedback_id IS '生成该候选题目的来源反馈 ID';
COMMENT ON COLUMN kb_eval_dataset.created_by IS '创建用户 ID';
COMMENT ON COLUMN kb_eval_dataset.created_at IS '创建时间';
COMMENT ON COLUMN kb_eval_dataset.updated_at IS '最后更新时间，编辑或归档后自动刷新';

COMMENT ON COLUMN kb_eval_result.id IS '评估结果主键';
COMMENT ON COLUMN kb_eval_result.dataset_id IS '关联评估问题 ID';
COMMENT ON COLUMN kb_eval_result.eval_version IS '评估版本号，对应文档 version';
COMMENT ON COLUMN kb_eval_result.actual_answer IS '模型实际回答';
COMMENT ON COLUMN kb_eval_result.hit IS '是否命中期望分块，NULL 表示不参与检索指标';
COMMENT ON COLUMN kb_eval_result.rank IS '期望分块命中排名';
COMMENT ON COLUMN kb_eval_result.faithfulness IS 'RAGAS Faithfulness 分数';
COMMENT ON COLUMN kb_eval_result.answer_relevancy IS 'RAGAS Answer Relevancy 分数';
COMMENT ON COLUMN kb_eval_result.context_recall IS 'RAGAS Context Recall 分数';
COMMENT ON COLUMN kb_eval_result.context_precision IS 'RAGAS Context Precision 分数';
COMMENT ON COLUMN kb_eval_result.status IS '评估状态：SUCCESS=评估成功，PARTIAL=部分完成或发生降级，FAILED=评估失败';
COMMENT ON COLUMN kb_eval_result.error_type IS '评估失败或降级类型';
COMMENT ON COLUMN kb_eval_result.duration_ms IS '整轮评估执行耗时，单位为毫秒；历史数据可为空';
COMMENT ON COLUMN kb_eval_result.eval_at IS '评估执行时间';

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
