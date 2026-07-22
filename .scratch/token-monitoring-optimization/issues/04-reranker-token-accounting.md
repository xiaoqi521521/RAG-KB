# 04 — 接入 Reranker Token 统计

**What to build:** 让成功的 Reranker provider 调用把 `usage.total_tokens` 纳入用户累计、Prometheus 监控和成本估算，同时保持现有的跳过、超时和 RRF 降级语义。Reranker 没有可靠 usage 时不能生成伪造成本。

**Blocked by:** 01 — 建立六类 Token 统计契约与最终回答路径

**Status:** resolved

- [ ] 成功且返回可靠 `usage.total_tokens` 的 Reranker 调用记录为 `reranker`。
- [ ] Reranker Token 写入当前用户 Redis v2，并带实际 Reranker 模型和单知识库/`multi` 知识库标签导出 Prometheus。
- [ ] 候选数不足跳过、超时、HTTP 错误、空结果和降级路径不记录未发生或未知的 Reranker Token。
- [ ] Reranker 成本使用配置的 `0.0005 元/1K Token`，不在业务代码中硬编码价格。
- [ ] Reranker 统计异常不改变 RRF 降级、回答、引用和权限行为。
- [ ] 覆盖成功 usage、跳过、超时、降级、Redis 故障、成本计算和指标标签测试。
