# 02 — 当前用户 Token 成本统计 API

**What to build:** 提供当前认证用户的累计在线问答 Token 与人民币成本估算接口。统计读取持久化 Redis 中的 Embedding、参考内容和最终用户回答生成 Token，使用部署配置的三类人民币单价进行 Decimal 计算，返回四位小数字符串金额和固定 `currency=CNY`。无统计返回零值，统计读取不可用返回 `503`，不提供跨用户聚合、时间分桶或账单能力。

**Blocked by:** None — can start immediately

**Status:** resolved

- [x] 提供认证 `GET /api/v1/stats/tokens`，只返回当前用户，不接受用户 ID、知识库 ID 或时间范围。
- [x] 响应包含 `embedding_tokens`、`context_tokens`、`generation_tokens`、`total_tokens`、`estimated_cost` 和 `currency`。
- [x] 成本单价通过部署配置提供，单位为人民币/1000 Token；不复制参考文件的硬编码模型价格。
- [x] 使用 Decimal 计算并将成本四舍五入为四位小数字符串，币种固定为 `CNY`。
- [x] 无历史统计返回三类 Token、总量为 0，成本为 `"0.0000"`；Redis 读取失败或数据不可解析返回 `503`。
- [x] 用户 Redis Hash 累计值不设置 TTL，生产持久化要求在设计与配置说明中明确。
- [x] 统计范围只覆盖在线查询 Embedding、裁剪后参考内容和最终用户回答生成 Token；不计离线索引、Reranker、内部查询改写/HyDE/忠实性调用。
- [x] provider 未返回生成 usage 时不使用本地猜测补写 Token；Token 累计失败不阻断正常问答。
- [x] 在 HTTP 和成本统计服务 seam 上覆盖当前用户隔离、零值、Decimal 计算、usage 缺失和 Redis 读取失败行为。

## Comments

- 2026-07-16：已实现 TokenMetrics 用户统计读取、Decimal 成本计算和 `GET /api/v1/stats/tokens`；Redis 读取失败返回 `503`，离线索引和内部质量调用不计入用户累计。
