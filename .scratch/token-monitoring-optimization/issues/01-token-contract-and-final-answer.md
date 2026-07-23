# 01 — 建立六类 Token 统计契约与最终回答路径

**What to build:** 让最终回答路径使用统一的 Token usage 记录契约，并对外提供六类用户累计 Token 和成本估算。同步回答与 SSE 回答都应把聊天输入和最终回答输出各记录一次，同时写入 Prometheus 运行指标和当前用户 Redis v2 累计。

**Blocked by:** None — can start immediately.

**Status:** resolved

- [x] 统一 Token usage 记录入口，接收模型、Token 类型和知识库归属范围，并将可靠 usage 分发到 Prometheus 与 Redis v2。
- [x] 支持 `embedding`、`input`、`answer_generation`、`hyde`、`reranker`、`faithfulness_check` 六类统计契约；本 Ticket 至少完整接通 `input` 和 `answer_generation`。
- [x] `input` 统计完整聊天模型输入，最终回答类型只统计 provider 输出；缺失或非法 usage 不用本地估算冒充精确值。
- [x] 同步与 SSE 最终回答各只记录一次聊天输入和输出；缓存命中不新增 Token。
- [x] 使用新版本 Redis 用户统计命名空间，不迁移旧三类字段。
- [x] `/api/v1/stats/tokens` 返回六类 Token、已累计 CNY 成本金额和固定币种；不返回六类 Token 的算术总量，成本使用配置单价及 Decimal 计算。
- [x] Redis 与 Prometheus 任一写入失败不阻断已经执行的问答，并产生无敏感标识的观测信号。
- [x] 覆盖 usage 提取、最终回答同步/SSE、Redis v2、成本计算、API 响应和 sink 故障测试。
