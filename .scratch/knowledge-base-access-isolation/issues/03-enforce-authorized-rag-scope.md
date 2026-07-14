# 03 — 强制 RAG 已授权知识库范围

**What to build:** 让同步与流式问答只在完全获准的知识库范围内运行。请求范围中任一知识库无读权限时整体拒绝；成功请求的向量和全文检索均只能返回当前版本、已完成且未删除文档的已授权 chunk。

**Blocked by:** 02 — 统一知识库读写授权

**Status:** resolved

- [x] 问答请求规范化并去重知识库范围，保留首次出现顺序。
- [x] 任一请求知识库无读权限时返回 `403`，且不创建会话、不调用 Embedding、检索、Reranker 或模型。
- [x] 同步与流式问答均使用相同的整体拒绝语义，不静默缩小请求范围。
- [x] 向量与全文检索均以完整已授权范围、当前文档版本、完成状态和未删除状态作为硬过滤条件。
- [x] RRF、Reranker、上下文裁剪、引用与生成只能消费已授权检索结果，不能扩大知识库范围。
- [x] 路由短路测试和仓储 SQL 行为测试共同验证以上约束。

## Comments

- 该范围边界已在现有请求 DTO、同步/流式路由与混合检索链路中实现；本次确认未引入重复实现。
- `uv run pytest tests/api/test_rag.py tests/api/test_chat.py tests/repositories/test_chunks.py tests/services/test_hybrid_retriever.py tests/services/test_enhanced_retriever.py -v` 与对应 Ruff 检查通过。
