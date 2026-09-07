-- 为所有业务字段补充 PostgreSQL 元数据注释；可安全重复执行。
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
DO $$
BEGIN
    IF EXISTS (
        SELECT 1
        FROM information_schema.columns
        WHERE table_schema = 'public'
          AND table_name = 'kb_answer_feedback'
          AND column_name = 'updated_at'
    ) THEN
        COMMENT ON COLUMN kb_answer_feedback.updated_at IS '最后更新时间，反馈更新后自动刷新';
    END IF;
END;
$$;

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
-- updated_at 由独立迁移创建；字段尚未创建时跳过，后续迁移会补充注释。
DO $$
BEGIN
    IF EXISTS (
        SELECT 1
        FROM information_schema.columns
        WHERE table_schema = 'public'
          AND table_name = 'kb_eval_dataset'
          AND column_name = 'updated_at'
    ) THEN
        COMMENT ON COLUMN kb_eval_dataset.updated_at IS '最后更新时间，编辑或归档后自动刷新';
    END IF;
END;
$$;

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
