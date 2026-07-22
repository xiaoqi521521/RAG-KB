# 02 — 接入 Embedding Token 统计

**What to build:** 让在线查询 Embedding 和离线索引 Embedding 都进入统一 Token 统计路径。在线 Embedding 归属当前用户，离线 Embedding 不归属用户但仍按模型和知识库范围进入 Prometheus，从而同时支持个人用量和平台运行监控。

**Blocked by:** 01 — 建立六类 Token 统计契约与最终回答路径

**Status:** resolved

- [ ] 查询 Embedding provider 返回的可靠 `total_tokens` 记录为 `embedding`。
- [ ] 在线 Embedding 写入当前用户 Redis v2，并使用当前请求的单知识库 `kb_id` 或多知识库 `multi` 标签导出 Prometheus。
- [ ] 离线索引 Embedding 不写用户 Redis，但在知识库范围可用时导出 Prometheus。
- [ ] Embedding 缓存命中、调用未发生或 provider usage 缺失时不伪造 Token。
- [ ] Embedding 模型标签使用实际配置模型，不记录用户、文档或对象路径标签。
- [ ] 覆盖在线、离线、缓存命中、usage 缺失、Redis 故障和 Prometheus 标签边界测试。
