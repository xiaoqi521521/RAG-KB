-- 导入历史数据后将序列定位到下一个可用 ID，避免首次写入主键冲突。
SELECT setval('kb_answer_feedback_id_seq', COALESCE(MAX(id), 0) + 1, false) FROM kb_answer_feedback;
SELECT setval('kb_chat_message_id_seq', COALESCE(MAX(id), 0) + 1, false) FROM kb_chat_message;
SELECT setval('kb_doc_chunk_id_seq', COALESCE(MAX(id), 0) + 1, false) FROM kb_doc_chunk;
SELECT setval('kb_document_id_seq', COALESCE(MAX(id), 0) + 1, false) FROM kb_document;
SELECT setval('kb_eval_dataset_id_seq', COALESCE(MAX(id), 0) + 1, false) FROM kb_eval_dataset;
SELECT setval('kb_eval_result_id_seq', COALESCE(MAX(id), 0) + 1, false) FROM kb_eval_result;
SELECT setval('kb_index_task_id_seq', COALESCE(MAX(id), 0) + 1, false) FROM kb_index_task;
SELECT setval('kb_knowledge_base_id_seq', COALESCE(MAX(id), 0) + 1, false) FROM kb_knowledge_base;
SELECT setval('kb_permission_id_seq', COALESCE(MAX(id), 0) + 1, false) FROM kb_permission;
