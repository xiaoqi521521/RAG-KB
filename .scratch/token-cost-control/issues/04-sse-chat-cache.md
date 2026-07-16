# 04 — SSE 聊天的首轮缓存与多轮绕过

**What to build:** 将首轮查询缓存接入 SSE 聊天。`GET /api/v1/chat/stream` 根据服务端实际历史决定是否使用缓存；缓存命中时保留现有会话协议，发送带 session ID 的首个状态、完整答案 `token` 和包含引用与本次耗时的 `done`，并在成功后持久化消息。多轮请求始终跳过缓存，继续执行完整流式 RAG。

**Blocked by:** 01 — 首轮普通 RAG 查询缓存

**Status:** resolved

- [x] 新会话或空会话的首轮 SSE 请求可以读取 Ticket 01 的查询缓存；存在任意历史时不读不写缓存。
- [x] 缓存命中事件顺序为 `status -> token -> done`，首个 status 包含活动 session ID，token 一次发送完整缓存答案。
- [x] `done` 返回缓存引用来源和本次完整服务 `latency_ms`，不返回缓存生成时的旧耗时。
- [x] 缓存命中成功保存 USER/ASSISTANT 消息，助手生成 `token_count=0`；消息持久化失败不得发送成功 `done`，也不得创建新的缓存条目。
- [x] 首轮缓存未命中成功后保存消息再写缓存；缓存写失败不改变已生成回答和 SSE 成功结果。
- [x] 多轮 SSE 继续使用最近五轮历史、既有 token/status/done/error 语义、超时和客户端断开处理。
- [x] 在 SSE HTTP/service seam 上覆盖缓存命中事件序列、session ID、引用与耗时、持久化失败、多轮绕过和 Redis 故障降级。

## Comments

- 2026-07-16：SSE 已按实际会话历史接入首轮查询缓存；命中保存零 Token 消息后发送完整 token/done，多轮请求绕过缓存，缓存写入失败保持成功流。
