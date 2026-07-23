# Token 成本控制与监控优化

Type: spec
Status: implemented

## Problem Statement

当前系统已经能够把 Embedding Token、Context Token 和最终回答生成 Token 按用户累计到 Redis，并输出日志，但这些 Token 观测主要停留在应用内部，无法稳定导出到 Prometheus，也没有 Grafana 面板、预算阈值和异常告警。

现有三类统计也不足以描述完整的在线 RAG 消耗：HyDE、Reranker、聊天模型完整输入和忠实性检测的消耗没有统一口径，部分调用点还会丢失 provider usage。现有用户统计字段不能区分完整聊天输入与仅供上下文裁剪使用的 `context_tokens`，因此无法为个人累计用量和成本估算提供一致的六类结果。

平台运维人员需要按模型、Token 类型和知识库范围查看消耗趋势，及时发现预算接近上限、单次异常大请求、provider usage 缺失和统计写入故障。普通用户则需要通过认证接口查看自己的六类累计 Token 和当前配置下的成本估算。该能力不要求账单级精确结算，也不需要保存单次请求明细。

## Solution

建立统一的 Token Usage Recorder，承接 Embedding、聊天输入、最终回答、HyDE、Reranker 和忠实性检测的 provider usage。每次确认到可靠 usage 后，同时写入 Prometheus Counter 和当前用户 Redis v3 累计；两个数据出口独立失败，不让监控写入故障阻断已经执行的问答。

Prometheus 通过现有 `/metrics` 出口提供 Token 指标，标签使用 `model`、`token_type` 和 `kb_id`。单知识库请求使用真实 `kb_id`，多知识库请求使用 `multi`，不重复复制或按比例估算同一批 Token。Grafana 连接 Prometheus 展示总览、Token 类型拆分、模型与知识库对比、成本估算和告警状态。Grafana/Prometheus 只供系统管理员与运维人员使用。

Redis 使用新的版本化用户统计命名空间，不迁移旧三类数据或没有金额字段的旧 v2 Hash。用户统计接口扩展为六类 Token 和人民币成本估算，不返回六类 Token 的汇总值。用户 Hash 同时保存六类 Token 和累计 `estimatedCostCny`，金额与 Token 字段在一次 Redis 原子更新中同步增加。成本单价来自环境配置；当前 Embedding、聊天输入、聊天输出单价取项目 `.env`，Reranker 使用百炼官方 `qwen3-rerank` 页面确认的 `0.5 元/百万 Token`，即 `0.0005 元/1K Token`。

增加全局每日 Token 预算闸门。预算默认是北京时间每日 `1,000,000 Token`，达到 80% 告警，达到 100% 拒绝后续新请求，已经开始的请求继续完成。单次请求实际总量超过 `20,000 Token` 时触发异常告警。告警本期只展示 Grafana 状态，不连接外部通知渠道。

## User Stories

1. As a 平台运维人员, I want to see Token consumption exported to Prometheus, so that application usage is available to standard monitoring infrastructure.
2. As a 平台运维人员, I want to view Token consumption in Grafana, so that I can inspect trends without reading application logs.
3. As a 平台运维人员, I want to filter Token panels by model, so that I can compare the cost contribution of Embedding, chat and Reranker models.
4. As a 平台运维人员, I want to filter Token panels by Token type, so that I can distinguish Embedding, chat input, final answer, HyDE, Reranker and faithfulness-check consumption.
5. As a 平台运维人员, I want to filter Token panels by knowledge base, so that I can identify which authorized knowledge base scope consumes the most resources.
6. As a 平台运维人员, I want a multi-knowledge-base request to use the `multi` scope label, so that one request is not counted multiple times across knowledge bases.
7. As a 平台运维人员, I want Prometheus labels to exclude user IDs, department IDs, request IDs, questions, answers and document IDs, so that metrics remain safe and bounded.
8. As a 系统管理员, I want Grafana and Prometheus access limited to system administrators and operations staff, so that operational consumption data is not exposed to ordinary users.
9. As a 普通知识库用户, I want to view only my own accumulated Token usage through an authenticated API, so that personal consumption is available without exposing global monitoring data.
10. As a 用户, I want to see accumulated Embedding Tokens, so that query and indexing-related online Embedding usage is visible for my requests.
11. As a 用户, I want to see complete chat input Tokens, so that System Prompt, question, history and retrieval context consumption is represented consistently.
12. As a 用户, I want to see final answer generation Tokens, so that the response generation cost is separated from prompt input cost.
13. As a 用户, I want to see HyDE Tokens, so that query expansion overhead is not hidden inside final answer usage.
14. As a 用户, I want to see Reranker Tokens, so that ranking overhead is included in my accumulated online usage.
15. As a 用户, I want to see faithfulness-check Tokens, so that quality-observation overhead is visible instead of being silently discarded.
16. As a 用户, I want the six Token buckets to be mutually exclusive, so that their sum does not double count the same provider usage.
17. As a 用户, I want to see an estimated CNY cost, so that I can understand accumulated monetary consumption without mixing Token types with different prices.
19. As a 用户, I want HyDE and faithfulness-check output to use the configured chat output price, so that internal chat-model calls are priced consistently.
20. As a 用户, I want Reranker usage to use its configured unit price, so that Reranker cost is not omitted from the estimate.
21. As a 平台运维人员, I want provider-reported usage to be preferred over local estimates, so that metrics do not present approximate counts as exact provider consumption.
22. As a 平台运维人员, I want missing or invalid provider usage to produce an explicit observability signal, so that I know when cost data may be underestimated.
23. As a 平台运维人员, I want cached model calls to produce no new Token usage, so that the dashboard reflects actual provider calls rather than logical requests.
24. As a 平台运维人员, I want offline Embedding without a current user to be excluded from user Redis totals, so that indexing consumption is not incorrectly charged to an online requester.
25. As a 平台运维人员, I want offline and online usage to remain visible in Prometheus where their knowledge-base scope is known, so that operational capacity planning includes indexing activity.
26. As a 平台运维人员, I want user statistics to start from a new versioned Redis baseline, so that incompatible legacy Context and Generation semantics are not silently merged.
27. As a 平台运维人员, I want Redis user totals to remain type-based rather than model-based for now, so that the first implementation does not introduce historical model price buckets.
28. As a 平台运维人员, I want to configure the daily global Token budget, so that the default budget can be adjusted for a deployment's capacity.
29. As a 平台运维人员, I want the daily budget to use Asia/Shanghai by default, so that the reset boundary matches the deployment's operating timezone.
30. As a 平台运维人员, I want the budget gate to warn at 80%, so that I have time to investigate before new traffic is blocked.
31. As a 平台运维人员, I want the budget gate to reject new requests at 100%, so that subsequent model calls do not continue after the global daily allowance is exhausted.
32. As a 用户, I want a request that started before budget exhaustion to finish, so that an in-flight answer is not corrupted by a later budget transition.
33. As a 平台运维人员, I want a request over 20,000 Tokens to trigger an alert, so that abnormal queries and implementation bugs are visible quickly.
34. As a 平台运维人员, I want budget rejection to be distinguishable from model failure, so that clients and operators can identify quota exhaustion correctly.
35. As a 平台运维人员, I want the budget gate to fail closed when its Redis state cannot be read, so that an infrastructure outage does not silently bypass the global budget.
36. As a 用户, I want ordinary Token statistic write failures not to fail my answer, so that observability degradation does not become a RAG availability failure.
37. As a 平台运维人员, I want Redis and Prometheus write failures to be separately observable, so that I can identify which data sink is degraded.
38. As a 平台运维人员, I want dashboards to show Token rates and daily increments, so that I can distinguish a sudden spike from historical cumulative volume.
39. As a 平台运维人员, I want dashboards to show estimated cost by Token type, model and knowledge-base scope, so that I can prioritize optimization work.
40. As a 平台运维人员, I want the dashboard to show budget usage and rejection counts, so that I can correlate consumption with user-visible throttling.
41. As a 平台运维人员, I want the dashboard to show usage-unavailable and sink-write-failure signals, so that gaps in the usage data are not mistaken for zero usage.
42. As a 平台运维人员, I want alerts to be visible in Grafana without a mandatory external notification integration, so that the first deployment remains infrastructure-neutral.
43. As a 开发人员, I want all usage-producing call sites to use one recorder seam, so that new model stages do not invent their own Redis fields and metrics.
44. As a 开发人员, I want the recorder to accept model, Token type and knowledge-base scope explicitly, so that labels are determined by business context rather than inferred from sensitive request data.
45. As a 开发人员, I want synchronous and streaming final answers to record usage once, so that streaming does not double count the completed response.
46. As a 开发人员, I want HyDE to preserve provider usage while still returning its cleaned answer text, so that query rewriting remains behaviorally compatible while becoming observable.
47. As a 开发人员, I want Reranker provider usage to be recorded only when reliable usage is returned, so that timeout and fallback paths do not invent ranking cost.
48. As a 开发人员, I want faithfulness observation to remain non-blocking for the answer path, so that added monitoring does not become a query availability dependency.
49. As a 开发人员, I want all metrics and logs to avoid questions, answers, prompts and document content, so that monitoring can be reviewed without leaking business data.
50. As a 维护人员, I want the solution to reuse the existing Prometheus endpoint and Redis client, so that the feature does not introduce an unnecessary observability stack.

## Implementation Decisions

- The highest application seam is a unified `TokenUsageRecorder` that receives normalized provider usage and fans it out to Prometheus, user Redis accumulation and the current request's in-memory accumulator.
- A separate `GlobalTokenBudgetGate` seam performs the global daily Redis budget check after authentication, full knowledge-base read authorization and cache eligibility checks, but before any provider call.
- The existing authenticated token statistics endpoint remains the public personal-usage seam and returns six Token fields, estimated CNY cost and currency; it does not return a cross-type Token sum.
- The six `token_type` values are `embedding`, `input`, `answer_generation`, `hyde`, `reranker` and `faithfulness_check`.
- `embedding` is Embedding provider input usage. `input` is complete chat-model input usage containing System Prompt, current question, conversation history and retrieval context. It does not include Embedding or Reranker input.
- `answer_generation`, `hyde` and `faithfulness_check` are output-only chat-model usage for their respective calls.
- `reranker` is the provider-reported Reranker usage, currently `usage.total_tokens`.
- Provider usage extraction accepts the existing LangChain usage metadata and compatible OpenAI response metadata. Missing, invalid or negative usage is not estimated locally and increments an unavailable-usage observation instead.
- Embedding, chat, HyDE, Reranker and faithfulness call sites all use the unified recorder. Cache hits and skipped provider calls do not record new Tokens.
- Prometheus Token metrics use the existing `prometheus_client` default registry and the existing `/metrics` endpoint. No OpenTelemetry Prometheus exporter is added for this feature.
- The core Token Counter is `rag_token_usage_total` with only `model`, `token_type` and `kb_id` labels. Other supporting metrics use only fixed low-cardinality labels and never use user, department, request, question, answer, document or object-path values.
- `kb_id` uses the real single knowledge-base ID for a single-scope request and `multi` for a request spanning multiple knowledge bases. Usage is never duplicated or proportionally allocated to individual knowledge bases.
- Grafana and Prometheus access is for system administrators and operations staff. The current `/metrics` network access behavior remains unchanged in this scope.
- User Redis statistics use a new v3 namespace with six Token fields and `estimatedCostCny`. Existing three-field data and v2 Hashes without the cost field are not migrated and do not contribute to the new baseline.
- User Redis totals are not split by model and no request-level Token or cost ledger is stored. Each Token field and the aggregate cost field are updated atomically; the aggregate cost uses the configured price at provider-call time and is not a provider-bill reconciliation.
- The current configured prices are Embedding `0.0005` CNY/1K Tokens, chat input `0.001` CNY/1K Tokens and chat output `0.002` CNY/1K Tokens. Reranker `qwen3-rerank` is `0.5` CNY/million Tokens, or `0.0005` CNY/1K Tokens, based on the official model page supplied during design.
- HyDE and faithfulness-check output use the chat output price. Reranker uses its dedicated configured price. Prices are environment configuration, not business-code constants.
- The daily global budget defaults to `1,000,000` Tokens and uses `Asia/Shanghai` by default with an overrideable timezone configuration.
- The budget uses a daily global Redis aggregate. An atomic gate rejects new requests once the recorded value has reached the configured budget. Requests already in progress complete and may cause a small observable overrun.
- Budget gate Redis failure returns `503` and prevents provider calls. Ordinary Token statistics write failure is non-blocking and is separately observable.
- Budget usage reaches Warning at 80% and Critical at 100%. A request whose actual total exceeds 20,000 Tokens increments an over-limit observation and produces a Grafana alert state.
- Budget rejection uses a distinct rate-limit response, recommended as `429 Too Many Requests`, with a message that the daily Token budget is exhausted.
- Prometheus/Grafana alert states are implemented without external notification channels in this scope.
- Redis atomic increments provide concurrency safety but not durable exactly-once accounting. Redis persistence, replication, backup and eviction policy are deployment responsibilities; the resulting totals are approximate operational statistics rather than billing-grade ledger data.
- The design does not introduce an independent tenant entity. Knowledge-base scope remains the existing logical isolation boundary; department remains an authorization-subject attribute and is not a metric label.

## Testing Decisions

- Tests should assert externally observable behavior at the highest available seams: the authenticated statistics API, the public RAG/chat boundary, the unified usage recorder contract and the budget gate contract.
- Tests should use controlled substitutes only for external provider, Redis and Prometheus boundaries. They should not assert private method order or add test-only production branches.
- Usage extraction tests cover Embedding total usage, chat input usage, chat output usage, Reranker total usage, compatible response metadata, missing usage and invalid values.
- Recorder tests cover all six type buckets, model and knowledge-base labels, `multi` attribution, current-user Redis writes, offline no-user behavior, zero/negative values and independent sink failures.
- Integration tests cover synchronous final answer recording, streaming final answer recording exactly once, HyDE usage preservation, Reranker success usage, Reranker timeout/fallback without invented usage and faithfulness usage in a non-blocking observation.
- Cache-hit tests prove that skipped provider calls do not increment any Token bucket or global budget usage.
- Redis schema tests prove the v3 namespace, six Token fields plus `estimatedCostCny`, zero defaults for new users and no migration of the old namespaces.
- Cost service tests verify persisted aggregate cost, Decimal rounding and fixed CNY currency without cross-type Token summation.
- API tests verify authentication, six response fields, cost without `total_tokens`, fixed CNY currency, Redis read failure `503` and current-user-only behavior.
- Budget gate tests verify Asia/Shanghai daily key selection, configurable budget, 80%/100% thresholds, atomic concurrent checks, rejection after exhaustion, completion of in-flight requests and Redis gate failure `503`.
- Request threshold tests verify that actual totals over 20,000 increment the alert counter without rejecting the completed request.
- Prometheus exposition tests verify that the `/metrics` output includes Token counters and supporting metrics with the allowed labels only.
- Prometheus rule tests or configuration validation verify budget Warning/Critical, single-request over-limit, usage-unavailable and sink-write-failure alert expressions.
- Grafana provisioning validation verifies the dashboard data source, required panels, variables for model/Token type/knowledge-base scope and visible alert states.
- Privacy tests inspect emitted labels and structured logs to ensure no user, department, request, question, answer, prompt, document or object-path values are present.
- Regression coverage must preserve existing permission hard filters, Reranker degradation, citation behavior, refusal behavior, streaming behavior and faithfulness non-blocking semantics.

## Out of Scope

- Request-level Token or cost detail, billing ledger, per-request audit history and exactly-once accounting.
- Model-specific Redis user buckets and historical price snapshots.
- Migration of old `embeddingTokens`, `contextTokens` or `generationTokens` values.
- Independent tenant entities, tenant columns, department metric tags, user metric tags and per-user/per-knowledge-base budgets.
- Allocation of multi-knowledge-base usage to individual knowledge bases.
- Provider-usage local approximation when provider usage is missing.
- Prometheus as the synchronous budget enforcement source.
- Network ACL changes for `/metrics`.
- Email, DingTalk, WeCom, Webhook or Alertmanager notification delivery.
- User-facing Grafana access, user-facing Prometheus query APIs or frontend Token dashboard work.
- Provider billing reconciliation, invoice generation and contract-level cost settlement.
- Changes to Embedding, HyDE, Reranker, retrieval, RRF, context trimming, citation, refusal, answer-generation or permission algorithms beyond adding usage instrumentation and budget gates.
- New database tables, database migrations or a separate metric database.
- A separate OpenTelemetry Prometheus exporter for this feature.

## Further Notes

- The detailed design and domain vocabulary are recorded in the repository's design output, `CONTEXT.md` and ADR-0006. This Spec is the implementation handoff and uses the established terms `用户 Token 成本统计`, `Token 监控指标`, `输入 Token`, `Token 归属范围`, `监控查看权限` and `全局 Token 预算`.
- The old Token cost-control plan describes the earlier three-type baseline and explicitly excluded Prometheus/Grafana and Reranker cost. This Spec supersedes those exclusions for the optimization scope while preserving the old plan as historical implementation context.
- The official Reranker price source supplied during design is the Bailian `qwen3-rerank` model page: <https://bailian.console.aliyun.com/cn-beijing?tab=model#/model-market/detail/qwen3-rerank?serviceSite=asia-pacific-china>.
- Redis persistence, replication, backup, no-eviction policy and alerting are deployment responsibilities. The application must expose sink failures so operations can see when accumulated statistics may be incomplete.
- This Spec is ready for implementation. No business code is changed by publishing it.
