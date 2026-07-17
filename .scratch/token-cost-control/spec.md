# Token 成本控制与监控

Type: spec
Status: ready-for-agent

## Problem Statement

企业知识库问答已经能够记录部分 Token 用量，也有文档 Embedding 缓存和上下文 Token 预算，但相同的首轮 FAQ 仍会反复执行查询改写、检索、Reranker 和回答生成。高频重复问题会产生不必要的模型成本和等待时间。

当前用户无法查看自己的累计 Token 消耗和人民币成本估算。已有 Token 数据只写入 Redis，没有公开读取接口；模型单价也没有配置边界。与此同时，多轮追问依赖对话历史，同一句当前问题在不同历史下可能得到不同答案，不能为了提高缓存命中率而复用错误上下文中的回答。

系统需要在不削弱实时权限校验、不改变完整 RAG 管道、不引入账单系统或监控平台扩建的前提下，为无历史首轮问答提供安全的短期结果缓存，并向当前用户提供清晰但明确为近似值的成本统计。

## Solution

为普通 RAG、同步聊天和 SSE 聊天增加共享的查询结果缓存能力。只有实际没有对话历史的首轮问答可以读取和写入缓存；任何多轮追问都继续携带最近五轮对话历史执行完整 RAG。缓存按去除首尾空白后的当前问题和排序后的知识库范围识别，在每次读取前重新完成身份加载与全部知识库读权限校验。

缓存仅保存带有效引用来源的成功回答，默认 10 分钟自然过期。缓存命中的聊天请求仍保存用户与助手消息，并返回本次完整服务实际耗时。Redis 缓存故障按未命中降级，不影响问答。

补充当前认证用户的累计 Token 与人民币成本估算接口。统计只覆盖在线问答的查询 Embedding、实际参考内容和最终用户回答生成 Token；成本单价由部署环境配置，金额以四位小数字符串返回并固定标识为 `CNY`。该统计用于成本趋势观察，不等同于供应商账单。

## User Stories

1. As a 知识库用户, I want a repeated first-turn FAQ to reuse a recent successful answer, so that I receive an answer faster and avoid unnecessary model usage.
2. As a 知识库用户, I want only requests without 对话历史 to use 查询结果缓存, so that a follow-up answer is never taken from another conversational context.
3. As a 知识库用户, I want my recent five rounds of 对话历史 to remain model input on cache-bypassed follow-ups, so that multi-turn answers preserve their intended context.
4. As a 知识库用户, I want a follow-up with even one prior message to execute the complete RAG pipeline, so that cache eligibility is based on actual history rather than a client hint.
5. As a 知识库用户, I want a new session with no messages to remain eligible for a first-turn cache hit, so that session creation does not remove the cache benefit.
6. As a 知识库用户, I want an existing but empty session to be treated as having no history, so that an earlier refusal or empty session does not force unnecessary model calls.
7. As a 知识库用户, I want leading and trailing whitespace not to create separate cache entries, so that cosmetically identical first-turn questions can be reused.
8. As a 知识库用户, I want changes in internal whitespace, letter case, punctuation or wording to remain distinct questions, so that the system does not conflate answers through aggressive normalization.
9. As a 知识库用户, I want the order of the same `kb_ids` not to affect cache identity, so that equivalent knowledge base ranges reuse the same answer.
10. As a 知识库用户, I want a different knowledge base range to produce a different cache identity, so that answers and 引用来源 never cross requested ranges.
11. As a 知识库用户, I want every cache lookup to occur only after my current identity and all requested knowledge base permissions are verified, so that a cached answer never bypasses access control.
12. As a 知识库用户, I want a multi-knowledge-base request with any unauthorized range to fail as a whole before cache access, so that the system never silently narrows my request.
13. As a 知识库用户, I want permission revocation to block my next cache lookup immediately, so that a previously visible answer does not grant continuing access.
14. As an authorized colleague, I want to reuse the same first-turn answer as another authorized colleague for the same knowledge base range, so that FAQ cache value is shared without duplicating user-specific entries.
15. As a 知识库用户, I want only a successful answer with 引用来源 to be cached, so that cached content remains verifiable.
16. As a 知识库用户, I want 拒答, no-hit results, invalid citations, timeouts and generation failures not to be cached, so that temporary content gaps and service faults are not replayed.
17. As a 知识库用户, I accept that a successful cached answer may remain stale for at most its 10-minute TTL after document changes, so that the first implementation can stay operationally simple.
18. As a 知识库用户, I want a cached synchronous chat answer to be saved as a normal user and assistant message pair, so that my 对话历史 remains complete.
19. As a 知识库用户, I want a cached SSE answer to retain the existing event contract, so that the frontend can render it without a separate protocol.
20. As a 知识库用户, I want the first SSE status to contain the active session identifier even on a cache hit, so that I can continue or reopen the session.
21. As a 知识库用户, I want the cached answer to arrive as a `token` event followed by `done`, so that existing stream consumers remain compatible.
22. As a 知识库用户, I want `done` to contain the cached 引用来源 and this request's actual latency, so that the final stream result remains complete and truthful.
23. As a 知识库用户, I want `latency_ms` to represent the complete executed business service path, including permission, cache and session work, so that a cache hit is not reported as a fake zero-latency response.
24. As a 知识库用户, I want a cached result to exclude the original request's latency and session identity, so that every response reflects the current request rather than another user's execution.
25. As a platform operator, I want cache reads to degrade to a miss when Redis is unavailable or contains an invalid value, so that the acceleration layer cannot make RAG unavailable.
26. As a platform operator, I want cache write failures not to change a valid answer into an error, so that successful model work is not discarded because an optional optimization failed.
27. As a platform operator, I want invalid cache values to be ignored and removed when possible, so that malformed or old entries do not poison later requests.
28. As a platform operator, I want cache logs to omit questions, answers, cache keys, user IDs and knowledge base IDs, so that observability does not leak sensitive or high-cardinality data.
29. As a 知识库用户, I want to view my accumulated online-query Embedding Token, 参考内容 Token and final-answer generation Token, so that I can understand my RAG usage.
30. As a 知识库用户, I want to see the arithmetic total of those three Token categories, so that the API is easy to consume.
31. As a 知识库用户, I want to see a four-decimal estimated cost in人民币 with `currency=CNY`, so that the estimate is explicit and stable for display.
32. As a 知识库用户, I want the statistics endpoint to return only my own totals, so that ordinary users and system administrators cannot use it to inspect another user's consumption.
33. As a 知识库用户, I want an account with no recorded usage to receive zero totals and `0.0000` estimated cost, so that absence of history is not treated as an error.
34. As a 知识库用户, I want the statistics endpoint to return service unavailable when Redis data cannot be read reliably, so that missing statistics are not misrepresented as zero usage.
35. As a platform operator, I want Embedding Token counted only when the online query actually calls the provider, so that Embedding cache hits do not create fictitious cost.
36. As a platform operator, I want final-answer generation Token counted only when provider usage is available, so that the system does not fabricate precision from a local guess.
37. As a platform operator, I accept that missing provider usage can make cost estimates lower than the actual bill, so that reported numbers remain evidence-based.
38. As a platform operator, I want 参考内容 Token measured from the content that actually enters answer generation, so that context cost follows the configured Token budget.
39. As a platform operator, I want System Prompt, current question and 对话历史 excluded from the initial chat-input estimate, so that the implementation retains the agreed approximate scope.
40. As a platform operator, I want offline indexing Embedding, Reranker, query-rewrite generation, HyDE generation and faithfulness observation excluded from user totals, so that costs are not arbitrarily assigned to an interactive user.
41. As a platform operator, I want the three Token prices supplied through deployment configuration, so that model-price changes do not require source-code changes.
42. As a platform operator, I want cost arithmetic to avoid binary floating-point drift, so that four-decimal人民币 output is deterministic.
43. As a platform operator, I want user Token totals retained without TTL in persistent Redis, so that normal application restarts do not reset cumulative statistics.
44. As a platform operator, I want Token accumulation failures not to block a normal answer, so that the observation layer remains non-critical to RAG availability.
45. As a product owner, I want this phase to stop short of administrator reports, time buckets and billing reconciliation, so that it delivers the agreed cost-control capability without becoming a finance platform.
46. As a product owner, I want this phase not to introduce Prometheus metrics, Grafana dashboards or alerts for cache and cost, so that monitoring expansion remains a separate decision.
47. As a developer, I want one query cache service to own key construction, serialization, TTL and Redis degradation, so that all RAG entry points use identical cache semantics.
48. As a developer, I want ordinary RAG, synchronous chat and SSE chat to retain their existing orchestration responsibilities, so that cache integration does not duplicate or hide the V4 RAG pipeline.
49. As a developer, I want the cache value validated through a structured response schema, so that incompatible or corrupt entries fail safely.
50. As a developer, I want the existing permission, Reranker degradation, citation, refusal, history and streaming behavior preserved, so that cost control does not regress answer correctness or isolation.

## Implementation Decisions

- Add one query result cache service that owns canonical cache identity, structured serialization, configured TTL, Redis reads and writes, invalid-value handling and failure degradation. It does not own permission decisions, session history, retrieval, answer generation, citations or transport behavior.
- A cache identity is the SHA-256 digest of a canonical structured representation containing `question.strip()` and ascending `kb_ids`. It does not include user, department, permission source, session or history.
- Do not perform case folding, punctuation removal, internal whitespace normalization, synonym expansion, embeddings or semantic similarity when building cache identity.
- The cache value has an explicit versioned schema containing `answer`, `sources` and `hit_count`. It excludes `latency_ms`, `session_id`, user data, history and Token usage.
- The cache TTL reuses the existing query-cache duration setting and defaults to 600 seconds. Document indexing, replacement, deletion and version publication do not actively invalidate entries in this phase.
- A response is cacheable only after answer generation and citation resolution succeed and at least one 引用来源 remains. 拒答, empty sources, invalid citations, timeout, disconnect and other failures do not create entries.
- Redis read, decoding or validation failure behaves as a cache miss. Redis write and dirty-entry deletion failures are non-fatal. Logs contain operation, outcome and error type only, without sensitive identifiers or content.
- Authentication and full knowledge-base range authorization remain outside the cache service and always run before cache access. A denied range short-circuits without revealing whether a cache entry exists.
- Ordinary RAG reads the cache after authorization and writes a successful cacheable response before returning. It continues to call the configured V1-V4 RAG pipeline on a miss rather than introducing a second retrieval path.
- Synchronous and SSE chat create or reuse the owned session and load actual history before deciding cache eligibility. Any existing history bypasses both cache read and write.
- A generated first-turn chat answer is persisted before creating a new cache entry. Cache write failure after persistence does not alter the successful response.
- A cache hit still persists one USER and one ASSISTANT message with the cached answer and sources, this request's actual service latency and generation `token_count=0`.
- SSE cache hits preserve the current named-event contract: initial `RETRIEVING` status with active session ID, one complete-answer `token`, then `done` with sources and actual latency. A successful `done` is sent only after message persistence.
- Full business-service timing starts at the question handler before permission checks and spans the executed permission, cache, history, session, retrieval and generation work. It excludes client network time and does not reuse cached latency.
- Continue storing current-user cumulative fields for Embedding, 参考内容 and final-answer generation Token in a Redis Hash without TTL. Persistent Redis is an operational prerequisite; these aggregates are not an immutable billing ledger.
- Only actual online query Embedding provider usage is attributed to the current user. Offline indexing has no user attribution.
- Only the Token of 参考内容 selected after context trimming contributes to approximate chat-input cost. System Prompt, current question and 对话历史 are deliberately excluded.
- Only provider-reported output Token from the final user-visible answer contributes to generation cost. Query rewriting, HyDE generation and faithfulness observation are excluded, and missing usage is not locally estimated.
- Token accumulation failure logs the category and error type and does not fail the answer. Statistics reads fail with service unavailable when Redis is unavailable or a stored value cannot be trusted.
- Add three required non-negative deployment settings denominated in人民币 per 1000 Token: Embedding input, chat input and chat output. Do not copy the reference document's model-specific prices as source-code defaults.
- Calculate cost with decimal arithmetic. `estimated_cost` is the sum of the three configured category costs, rounded to four decimal places and serialized as a string. The response always includes `currency` with the fixed value `CNY`.
- Add authenticated `GET /api/v1/stats/tokens`. It accepts no user selector, knowledge-base selector or time range and returns only the current user's `embedding_tokens`, `context_tokens`, `generation_tokens`, `total_tokens`, `estimated_cost` and `currency`.
- A user without stored statistics receives zero Token values and `estimated_cost="0.0000"`. Authentication retains existing `401` and identity-source `503` semantics; unreadable Redis statistics return `503`.
- Do not add database migrations. Query responses remain in Redis, and existing user Token Hashes remain the cumulative store.
- Do not add an ADR for these service-level, reversible policies. The canonical domain terms are 查询结果缓存, 用户 Token 成本统计, 问答服务耗时, 成本估算单价, 近似聊天输入成本 and 可用 Token 用量.

## Testing Decisions

- Tests verify behavior through public interfaces and stable service contracts, not private methods, internal helper order or testing-only production branches.
- The primary seam is the existing HTTP/SSE API boundary. It covers authorization before cache access, ordinary RAG cache reuse, chat history eligibility, session persistence, SSE event order and payloads, actual latency and current-user statistics.
- API tests use the established FastAPI dependency-override style with fakes at external boundaries. They assert responses and observable short-circuit behavior rather than coupling to internal cache implementation.
- Verify that a repeated ordinary RAG first turn executes the RAG dependency once, then returns the same answer and sources from cache with a newly measured latency.
- Verify that cached data cannot be read until all requested knowledge bases pass permission checks, including whole-request rejection for one unauthorized ID and immediate denial after permission revocation.
- Verify cross-user reuse only when both users independently pass real-time authorization for the same knowledge base range.
- Verify a new or empty chat session can hit the cache, persists a normal message pair and stores zero generation Token for that request.
- Verify any non-empty history bypasses cache reads and writes, injects the recent history into generation and preserves the existing full RAG behavior.
- Verify SSE cache-hit events are `status -> token -> done`, the status exposes the active session ID, the token carries the full answer, and `done` carries sources and this request's latency.
- Verify message-persistence failure prevents a successful SSE `done` and does not create a new cache entry.
- Verify failures and explicit 拒答 do not create a cache entry and that Redis cache failure falls back to the complete RAG pipeline.
- Verify the statistics endpoint is authenticated, reads only the current user, returns zero values for absent data, formats decimal cost and fixed currency correctly, and returns `503` for unreadable statistics.
- The secondary seam is the public query-cache and cost-statistics service interfaces. It is limited to deterministic behavior that is difficult to diagnose precisely through the API.
- At the query-cache service seam, verify the known SHA-256 identity for a fixed question and knowledge-base set, order independence, intentionally distinct question variants, TTL, structured round trip, dirty-value handling and non-fatal Redis failures.
- At the cost-statistics service seam, verify a worked literal cost example, four-decimal rounding, missing-user zeros, malformed stored values and Redis read failure. Expected amounts must be fixed independent literals rather than recomputing the production formula inside the test.
- Preserve existing Token tests that distinguish provider usage from cache hits and unavailable usage. Add coverage proving offline indexing and internal quality-model output do not enter the current-user aggregate.
- Preserve existing RAG, permission, synchronous chat and streaming tests as regression coverage for retrieval filters, Reranker fallback, 引用来源, 拒答 and 对话历史.
- Completion validation runs focused cache, Token, cost, RAG, chat and stats tests first, then the full test suite, Ruff and Mypy.

## Out of Scope

- Semantic caching, fuzzy matching, case-insensitive matching, punctuation normalization or history-aware cache identity.
- Caching any multi-turn follow-up, even when its current question text matches a first-turn question.
- Active cache invalidation from document indexing, replacement, deletion or version changes; knowledge-base content revision keys and invalidation events.
- Caching 拒答, empty-reference answers, partial streams, timeouts, disconnects or other failures.
- Changing query rewriting, HyDE, hybrid retrieval, RRF, Reranker, confidence filtering, context trimming, citation resolution, refusal rules or faithfulness evaluation.
- Attributing offline indexing, Reranker or internal quality-model costs to a user.
- Counting complete model input Token, System Prompt, current question or 对话历史 in approximate chat-input cost.
- Administrator aggregation, cross-user lookup, knowledge-base cost allocation, daily or monthly buckets, retention jobs, exports, quotas, budgets, invoicing or provider-bill reconciliation.
- New database tables, immutable accounting records or recovery of historical totals after Redis data loss.
- Token, cost or cache Prometheus metrics, OpenTelemetry exporter changes, Grafana dashboards and alerts.
- Frontend cost pages, cache indicators or new stream event types.
- Changing the configured chat, Embedding or Reranker models.

## Further Notes

- Query result caching is an optimization after authorization, never an authorization source. It must not weaken knowledge-base-scoped logical isolation or session ownership.
- The accepted 10-minute stale window is deliberate. A later consistency requirement should introduce content revisions or explicit invalidation as a separate design rather than silently changing this contract.
- User Token cost statistics are cumulative operational estimates. They can be lower than actual supplier charges because the agreed scope excludes complete prompt input, internal model calls, Reranker and unavailable usage.
- Redis persistence protects normal restarts but does not make the data an audit-grade ledger. The API must not present these totals as billing truth.
- The detailed Plan remains the module-level design record. This tracker Spec is the implementation scope and is ready to be split into tracer-bullet tickets.
