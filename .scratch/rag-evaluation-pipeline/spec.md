# RAG 效果评估与反馈闭环

Type: spec
Status: ready-for-agent

## Problem Statement

企业知识库当前已经具备完整的 V4 RAG 查询链路和在线抽样忠实性观测，但缺少一套可重复、可比较的正式效果评估机制。知识库管理员无法维护标准问题集，无法用固定指标衡量一次已部署配置的检索和生成质量，也无法按评估版本比较历史结果。团队只能依靠人工试问判断效果，无法回答一次检索、精排、上下文或模型调整是否真正改善了质量。

现有评估数据表只具备参考项目的基础字段，不能表达数据集候选、文档重建后的标注失效、Context Recall、Context Precision、单题部分失败或明确的指标分母。参考实现还会对同一问题执行两次 RAG 查询，使检索指标与生成回答可能来自不同候选和上下文，不能作为可信的评估证据。

用户点赞和点踩尚未形成可治理的反馈闭环。系统已经持久化用户问题、助手回答和引用，但没有安全的反馈入口，也不能把单知识库差评转为待人工审核的评估候选。与此同时，文档重建会删除旧 chunk，使标准问题中的期望 chunk ID 失效；如果系统继续评估这些问题，历史趋势将被静默污染。

项目还缺少统一请求 Trace ID，评估、反馈和普通 RAG 请求无法在日志中可靠关联。上述能力必须遵守知识库级逻辑隔离、会话所有权、敏感内容日志限制和既有 Reranker 降级语义，不能通过 Prompt、前端约束或复制查询管道实现。

## Solution

在现有 V4 RAG 能力上增加知识库级效果评估和用户反馈闭环。知识库管理员维护带状态的标准问题集，查询当前有效 chunk 摘要并标注期望 chunk 或期望答案，然后通过同步接口运行当前已部署的真实 V4 管道。每个问题只执行一次 RAG；共享执行结果同时提供 Reranker 排名、实际参考内容、实际回答和公开响应，供线上问答与评估复用。

检索质量使用 Hit Rate@5 和 MRR@5，生成质量使用 RAGAS 的 Faithfulness、Answer Relevancy、Context Recall 和 Context Precision。Reranker 发生任何降级时，线上回答仍使用 RRF 结果正常生成，但该题跳过全部检索与 RAGAS 指标。明确拒答跳过四项生成指标。所有不可用指标保持为空，不按零分处理；报告为每项指标返回独立样本数。

评估按知识库和唯一评估版本同步执行，逐题隔离故障，全部问题结束后一次事务保存结果并返回聚合报告。不新增评估运行表或期望证据关系表。文档新版本发布时，系统在删除旧 chunk 前通过期望 chunk ID 数组识别受影响标准问题并标记为待审核。

用户可以对自己会话中的助手消息提交或覆盖点赞、点踩和评论。单知识库差评自动生成评估候选，多知识库或无法确认范围的差评只保留反馈。候选通过反馈关系追溯用户实际看到的回答和引用，但不重复保存回答正文。通用请求中间件为所有响应提供 `X-Trace-Id`，日志和指标继续遵守敏感内容及高基数限制。

## User Stories

1. As a 知识库管理员, I want to maintain a 标准问题集, so that evaluation uses agreed questions rather than ad hoc manual testing.
2. As a 知识库管理员, I want each standard question to belong to one knowledge base, so that evaluation never mixes unrelated knowledge boundaries.
3. As a 知识库管理员, I want to record one or more expected chunk IDs, so that retrieval quality can be measured against explicit evidence.
4. As a 知识库管理员, I want expected chunk IDs to support chunks from multiple documents, so that questions requiring combined evidence can be evaluated.
5. As a 知识库管理员, I want to record an expected answer, so that generation and context quality can be evaluated.
6. As a 知识库管理员, I want a standard question to participate in retrieval metrics, generation metrics, or both according to its labels, so that partial annotation remains useful.
7. As a 知识库管理员, I want to list current valid chunks with document and location metadata, so that I can choose accurate expected chunk IDs.
8. As a 知识库管理员, I want chunk summaries capped at a short excerpt, so that annotation does not require returning full document bodies.
9. As a 知识库管理员, I want the first chunk-query version to return the complete matching list without pagination, so that the initial workflow remains simple.
10. As a 知识库管理员, I want to create, edit and archive standard questions, so that the evaluation set can evolve.
11. As a 知识库管理员, I want deletion to archive rather than physically remove a question, so that historical results remain traceable.
12. As a 知识库管理员, I want an already evaluated question to be immutable, so that old results never point to changed questions or labels.
13. As a 知识库管理员, I want to replace an evaluated question by archiving it and creating a new one, so that history and future evaluation both remain correct.
14. As a 知识库管理员, I want only active standard questions included in formal evaluation, so that candidates and stale labels cannot affect metrics.
15. As a 知识库管理员, I want candidates, active questions, questions needing review and archived questions to have distinct states, so that their lifecycle is explicit.
16. As a 知识库管理员, I want candidate and review-state promotion to remain a database operation in the first release, so that no unrequested state-transition endpoint is introduced.
17. As a 知识库管理员, I want document reindexing to mark affected active questions as needing review, so that deleted chunk IDs are never silently treated as current truth.
18. As a 知识库管理员, I want failed document rebuilding to leave standard questions unchanged, so that the current published document remains evaluable.
19. As a 知识库管理员, I want label invalidation to commit with successful document-version publication, so that published content and standard-question validity cannot diverge.
20. As a 质量评估人员, I want each question to execute the real deployed V4 pipeline exactly once, so that ranking, context and answer come from one coherent execution.
21. As a 质量评估人员, I want Hit Rate@5, so that I can measure whether expected evidence appears in the final top-five reranked candidates.
22. As a 质量评估人员, I want MRR@5, so that I can distinguish early expected evidence from evidence ranked near the cutoff.
23. As a 质量评估人员, I want multiple expected chunks to use the best matching rank, so that combined-evidence questions receive a stable reciprocal rank.
24. As a 质量评估人员, I want retrieval misses to contribute zero reciprocal rank, so that MRR reflects real misses.
25. As a 质量评估人员, I want questions without expected chunks excluded from retrieval denominators, so that missing annotation is not counted as failure.
26. As a 质量评估人员, I want all Reranker-degraded questions excluded from retrieval metrics, so that RRF fallback is not presented as Reranker quality.
27. As a 质量评估人员, I want Faithfulness measured against the actual reference content, so that unsupported answer claims reduce the score.
28. As a 质量评估人员, I want Answer Relevancy measured from the actual answer, so that verbose but off-topic answers are visible.
29. As a 质量评估人员, I want Context Recall measured against the expected answer, so that missing supporting context is visible.
30. As a 质量评估人员, I want Context Precision measured against the expected answer, so that noisy reference content is visible.
31. As a 质量评估人员, I want generation metrics to use only content actually supplied to the answer model, so that removed or truncated text is not credited as evidence.
32. As a 质量评估人员, I want questions without expected answers excluded from generation denominators, so that missing labels do not become zero scores.
33. As a 质量评估人员, I want explicit refusals excluded from RAGAS generation metrics, so that empty-context behavior does not produce misleading `NaN` or artificial scores.
34. As a 质量评估人员, I want refusal count and refusal rate in reports, so that excessive refusal remains visible despite null generation metrics.
35. As a 质量评估人员, I want Reranker degradation to skip all RAGAS metrics, so that degraded retrieval does not produce a generation-quality score presented as normal evaluation.
36. As a 质量评估人员, I want each metric to report its own sample count, so that averages with different valid populations are interpreted correctly.
37. As a 质量评估人员, I want unavailable metric values to remain null, so that failures and non-applicable samples are never silently converted to zero.
38. As a 质量评估人员, I want each question classified as successful, partial or failed, so that report completeness is visible.
39. As a 质量评估人员, I want one metric failure not to erase other valid metrics, so that partial evaluation remains useful.
40. As a 质量评估人员, I want one question failure not to stop later questions, so that isolated provider failures do not discard an entire dataset.
41. As a 质量评估人员, I want all-question failure to return a saved failure report, so that dependency outages are observable rather than mistaken for missing runs.
42. As a 质量评估人员, I want one unique evaluation version per knowledge base, so that unrelated runs are never aggregated under the same label.
43. As a 质量评估人员, I want evaluation versions to label the current deployment rather than override it, so that the evaluated behavior matches production.
44. As a 质量评估人员, I want historical reports ordered by evaluation time, so that I can compare deployed versions.
45. As a 质量评估人员, I want historical reports to contain aggregates rather than full per-question records, so that the first release stays within the agreed interface scope.
46. As a 平台运维人员, I want four RAGAS metrics for one question to run concurrently, so that synchronous evaluation does not add avoidable serial latency.
47. As a 平台运维人员, I want each RAGAS metric to have a 30-second timeout, so that one external call cannot wait indefinitely.
48. As a 平台运维人员, I want each retryable RAGAS failure retried at most once, so that transient errors can recover without uncontrolled cost.
49. As a 平台运维人员, I want non-retryable RAGAS errors and invalid scores to fail immediately, so that malformed results are not repeatedly submitted.
50. As a 平台运维人员, I want the evaluation to reuse the existing answer and embedding clients, so that no additional provider credentials or routing are required.
51. As a 平台运维人员, I want all results written in one final transaction, so that a completed version never contains only part of its question set.
52. As a 平台运维人员, I want an interrupted in-memory run to leave no results, so that the same version label can be retried.
53. As a 平台运维人员, I want synchronous manual triggering only in the first release, so that no scheduler, worker or CI gate is introduced.
54. As a 企业员工, I want to submit a useful or not-useful response for my own assistant message, so that I can signal answer quality.
55. As a 企业员工, I want to update my previous feedback, so that an accidental click does not create duplicate feedback.
56. As a 企业员工, I want an optional feedback comment, so that I can explain why an answer was not useful.
57. As a 企业员工, I want feedback to remain attached to the exact assistant answer I saw, so that later analysis does not confuse it with a regenerated answer.
58. As a 企业员工, I want feedback on another user's conversation to be treated as not found, so that message identifiers cannot expose private conversations.
59. As a 知识库管理员, I want a single-knowledge-base downvote to create one candidate question, so that real user failures can improve the evaluation set.
60. As a 知识库管理员, I want the candidate to trace back to the source feedback and assistant answer, so that I can understand why it was proposed.
61. As a 知识库管理员, I want the original assistant answer to stay in chat-message storage rather than be duplicated in evaluation data, so that there is one authoritative copy.
62. As a 知识库管理员, I want a downvote changed to an upvote to archive an unreviewed candidate, so that stale negative signals leave the candidate queue.
63. As a 知识库管理员, I want an upvote changed back to a downvote to restore the same candidate, so that feedback updates remain idempotent.
64. As a 知识库管理员, I want active or review-state questions unaffected by later feedback changes, so that a user cannot automatically alter the formal evaluation set.
65. As a 系统管理员, I want multi-knowledge-base or unassigned feedback stored without automatic candidate creation, so that the system does not guess an incorrect knowledge base.
66. As a 系统管理员, I want to choose a target knowledge base manually for such feedback, so that cross-knowledge-base questions receive deliberate governance.
67. As a 安全审查人员, I want evaluation management restricted to system administrators or local knowledge-base administrators, so that readers and document writers cannot alter quality baselines.
68. As a 安全审查人员, I want every dataset operation scoped by both knowledge base ID and dataset ID, so that global identifiers cannot bypass knowledge-base isolation.
69. As a 安全审查人员, I want expected chunks validated against the target knowledge base, current document version, completed status and deletion state, so that labels cannot reference inaccessible or stale evidence.
70. As a 安全审查人员, I want evaluation retrieval to remain hard-filtered to one authorized knowledge base, so that evaluation cannot expose another knowledge base through models or reports.
71. As a 安全审查人员, I want logs and metrics to omit questions, answers, comments, chunks, prompts and sensitive resource identifiers, so that observability does not leak enterprise content.
72. As a 平台运维人员, I want every request to have an `X-Trace-Id`, so that evaluation, feedback and RAG failures can be correlated across logs.
73. As a 平台运维人员, I want safe client Trace IDs reused and unsafe values replaced, so that trace propagation cannot inject arbitrary log content.
74. As a 平台运维人员, I want Trace ID returned in the response, so that callers can report the exact request correlation value.
75. As a 平台运维人员, I want Trace ID excluded from metric labels, so that monitoring does not create unbounded cardinality.
76. As a developer, I want one shared RAG execution interface for online queries and evaluation, so that retrieval and generation behavior are maintained in one place.
77. As a developer, I want internal ranking and reference context excluded from the public RAG response, so that evaluation diagnostics do not change the client contract.
78. As a developer, I want deterministic retrieval metrics separated from the RAGAS adapter, so that metric math can be tested without models or databases.
79. As a developer, I want RAGAS integration hidden behind one adapter interface, so that library compatibility, retries and score validation remain local.
80. As a developer, I want an import smoke test for the locked RAGAS stack, so that dependency resolution cannot be mistaken for runtime compatibility.

## Implementation Decisions

- The feature uses the established domain terms 标准问题集, 评估候选项, 待审核标准问题, 评估运行, 评估版本, 检索评估, 生成评估 and 回答反馈.
- The implementation does not add a database table. It extends the existing evaluation dataset, evaluation result, chat message and answer feedback persistence models.
- Standard-question status is `CANDIDATE`, `ACTIVE`, `NEEDS_REVIEW` or `ARCHIVED`. Only `ACTIVE` participates in a formal evaluation.
- Existing standard-question rows migrate to `ACTIVE`. Dataset queries are indexed by knowledge base and status.
- A standard question retains optional expected answer and expected chunk ID array. Chunk IDs are globally unique primary keys and may identify chunks from multiple documents.
- The dataset stores a nullable review reason and nullable unique source feedback ID. The feedback link prevents duplicate candidates and supports tracing to the original assistant message.
- Dataset create, list, edit and archive interactions remain public to evaluation administrators. Requests cannot set status, creator identity or feedback source.
- Manual standard-question creation defaults to `ACTIVE` and requires a nonblank question plus at least one expected answer or expected chunk label.
- Candidate and review-state promotion to `ACTIVE` has no application endpoint in this release. Direct database promotion must be preceded by validation of labels and current chunk ownership.
- Archive replaces physical deletion for every dataset state and remains idempotent.
- Once a dataset row has an evaluation result, its question and labels are immutable. A correction archives the old row and creates a new row.
- Document-version publication reads the old version's chunk IDs before deleting them. Active dataset rows whose expected chunk arrays overlap those IDs become `NEEDS_REVIEW` with a stable reindex reason in the same database transaction as publication.
- Failed or abandoned reindexing does not alter standard-question status.
- Evaluation result Hit is nullable. Null means the question did not participate in retrieval metrics, not that it missed.
- Evaluation result includes nullable rank, Faithfulness, Answer Relevancy, Context Recall and Context Precision, plus required result status and nullable low-cardinality error type.
- Result status is `SUCCESS`, `PARTIAL` or `FAILED`. Missing labels are non-applicable and do not by themselves make a result partial.
- Reranker degradation leaves every metric null, sets result status to `PARTIAL`, and uses `reranker_degraded` as the stable error type. Specific degradation reasons are retained only in safe logs.
- Metric failures use a stable generic RAGAS error type in persistence. Specific failed metrics and provider reasons remain in safe logs rather than being concatenated into a database string.
- Metric values must be finite and within zero to one. Rank must be one through five when present.
- A dataset ID and evaluation version pair is unique as a database concurrency backstop.
- Evaluation version is unique within one knowledge base and identifies exactly one completed synchronous evaluation. It is a label, not a pipeline selector or runtime configuration override.
- The evaluation executes the currently deployed V4 pipeline for one authorized knowledge base.
- A shared RAG execution module exposes one internal `execute` interface to online V4 orchestration and formal evaluation. It returns the public response, final reranked candidates, actual reference content, internal Reranker degradation state and internal refusal state.
- The internal execution interface is the principal implementation and test seam. Its diagnostic result is never serialized into the public RAG response.
- Reference content exposed internally to evaluation is exactly what entered the answer model. Character or token truncation is preserved, and the evaluation does not use removed full chunk text, a second retrieval, or only the answer's cited subset.
- Online V4 calls the shared execution interface, returns the existing public response and schedules the existing online sampled faithfulness observation.
- Formal evaluation calls the same execution interface but does not schedule online sampled faithfulness, preventing duplicate evaluation.
- Hit Rate@5 and MRR@5 use successful Reranker output before confidence filtering and context trimming. The top-five cutoff is fixed for this release.
- Hit is true when any expected chunk appears in the top five. Reciprocal rank uses the first expected chunk rank; a valid non-hit contributes zero.
- Questions without expected chunks and all Reranker-degraded questions have null Hit and rank and are excluded from retrieval denominators.
- A normal empty retrieval is an evaluable miss when no Reranker degradation occurred.
- Faithfulness uses question, actual answer and actual reference content. Answer Relevancy uses question and actual answer. Context Recall and Context Precision use question, expected answer and actual reference content.
- Generation evaluation requires a nonblank expected answer, a non-refusal answer and no Reranker degradation.
- Explicit refusal does not invoke the four RAGAS metrics. Its generation metrics remain null and its actual answer is the project fixed refusal text.
- Refusal count is aggregated by fixed actual-answer equality. Refusal rate divides refusal count by total questions for that version; no new refusal column is added.
- Any Reranker degradation continues online answer generation from the RRF fallback but skips Hit, MRR and all RAGAS evaluation for that question.
- The RAGAS adapter reuses the existing answer-model and `text-embedding-v3` clients. It does not add an evaluation-specific provider, key or model route.
- The four applicable RAGAS metrics execute concurrently within one question. Questions themselves execute sequentially.
- Each metric has a 30-second timeout and at most one retry for timeout, rate limit or temporary provider failure. Invalid input, parse failure, `NaN`, infinity or out-of-range score does not retry.
- One metric failure leaves only that metric null and permits other valid values to persist. One question main-flow failure creates a `FAILED` result and does not stop later questions.
- Evaluation is manually triggered through a synchronous HTTP interaction. It loads all active questions without pagination, count limit or overall run timeout.
- Results accumulate in memory and are inserted in one final transaction after every question has completed. Process interruption or final persistence failure leaves no partial evaluation version and permits retry with the same version label.
- A run whose every question failed still saves the results and returns a successful HTTP response with zero successes and the failed count. Database, authorization, missing-dataset and version-conflict failures remain HTTP errors.
- Historical evaluation returns version aggregates ordered by evaluation time. It does not expose a per-question history interaction.
- Every aggregate returns total, success, partial and failure counts; retrieval sample count, hit count, Hit Rate@5 and MRR@5; an independent sample count and average for each RAGAS metric; refusal count, refusal rate and evaluation time.
- Null metrics are excluded from their own aggregate denominator. A metric with no sample returns null rather than zero.
- Reports are computed from result rows and are not separately persisted. Different versions may use different dataset rows, so sample counts are required for interpretation and no intersection-only comparison is added.
- The evaluation management interactions are synchronous run, version history, dataset list/create/edit/archive and current chunk summary list under the existing versioned API namespace.
- The chunk summary interaction returns current-version chunks from completed, non-deleted documents. It returns chunk and document identity, document name, chunk index, page, section, token count and an excerpt capped at 200 characters. It is intentionally not paginated in this release.
- Evaluation management requires either system administrator authority or local `ADMIN` authority for the target knowledge base. Public read, `READ` and `WRITE` do not grant evaluation management.
- Evaluation management adds an explicit knowledge-base administrator check to the existing permission module and preserves `404`, `403` and `503` semantics.
- Every dataset lookup or mutation scopes by knowledge base ID and dataset ID. Expected chunk validation requires the same knowledge base, current document version, completed document status and non-deleted state; one invalid ID rejects the entire write.
- Formal evaluation passes exactly one authorized knowledge base to the V4 pipeline. Retrieval and all downstream stages preserve that hard scope.
- A chat assistant message stores the full knowledge base range for its own turn as a nullable array. Historical messages may be null and cannot automatically create a candidate when scope is unknown.
- The design does not add an explicit user-message parent ID. Feedback resolves the nearest preceding user message in the same session using creation time and message ID ordering.
- A user may submit feedback only for an assistant message in the user's own non-deleted conversation. Missing, foreign and non-assistant messages all return not found.
- Feedback is constrained to `1` or `-1` and is unique by assistant message and user. A repeated submission updates the existing feedback and the assistant message's feedback value in one transaction.
- Feedback persistence stores only message identity, user identity, value and optional comment. The original answer and sources remain in chat-message storage; formal evaluation answers remain in evaluation-result storage.
- A downvote on a single-knowledge-base turn creates or restores one `CANDIDATE`. A downvote on a multi-knowledge-base or unknown-scope turn records feedback only.
- Changing a downvote to an upvote archives the linked row only while it remains `CANDIDATE`. Changing back to a downvote restores that row. Formal lifecycle states never change automatically from later feedback.
- Multi-knowledge-base candidate assignment is a system-administrator database operation in this release.
- A request middleware accepts a safe `X-Trace-Id` or creates a UUID value, stores it in request-local logging context, returns it in the response and always clears context after the request.
- Trace ID is allowed in logs but forbidden as a metrics label. Logs and metrics must not contain JWTs, full questions, expected answers, actual answers, feedback comments, reference content, chunk text, prompts, credentials or high-cardinality business identifiers.
- Low-cardinality observability covers evaluation outcome, question outcome, RAGAS result category, Reranker degradation, feedback polarity and candidate changes. Existing token observation remains operational but token and latency are not persisted per evaluation version.
- The implementation must first lock a RAGAS/LangChain combination that imports and executes successfully under `uv`. It must update dependency metadata and the lockfile and must not patch the virtual environment or inject a fake compatibility module.

## Testing Decisions

- The highest test seam is the shared RAG execution interface. Exercise one execution and assert its public response, reranked top-five candidates, actual reference content, degradation state and refusal state without testing private orchestration helpers.
- The main evaluation behavior test crosses the formal evaluation interface with controllable adapters for the shared RAG execution and RAGAS provider. It asserts externally observable result rows and aggregate reports rather than internal method call order.
- Public FastAPI behavior is the highest seam for authentication, knowledge-base administration, request validation, response status, aggregate response shape, feedback ownership and Trace ID propagation.
- Repository tests are reserved for non-bypassable database behavior: knowledge-base-scoped dataset access, current chunk validation, array-overlap invalidation, version conflict, unique feedback, final batch persistence and aggregate SQL.
- Pure Hit@5 and MRR@5 behavior is tested through one deterministic metric interface with chunk ID lists. It does not require database, model or network mocks.
- The RAGAS adapter is tested through its public evaluation interface. The model and embedding providers are external adapters and may use deterministic fakes; RAGAS internals and private helpers are not mocked.
- Add a runtime import smoke test for the exact locked RAGAS types used by production. A successful dependency resolver run is not sufficient.
- Shared execution tests verify that online and formal evaluation each cause one retrieval/rerank/generation execution, that actual truncated reference content is exposed internally, and that diagnostics do not appear in the public response.
- Shared execution regression tests preserve current authorization, HyDE degradation, hybrid retrieval, RRF, Reranker fallback, confidence filtering, context trimming, citation resolution, refusal and online faithfulness observation behavior.
- Retrieval metric tests cover expected chunks at rank one and five, rank six, no hit, multiple expected chunks, empty results and the fixed top-five boundary.
- Evaluation orchestration tests verify that missing expected chunks produce null retrieval fields without partial status, and missing expected answer produces null generation fields without partial status.
- Evaluation orchestration tests verify that every Reranker degradation keeps the generated RRF answer, sets all metrics null, skips the RAGAS adapter, sets `PARTIAL` and persists the stable degradation error type.
- RAGAS tests cover all four input shapes, concurrent execution, 30-second timeout, one retry, retry classification, parse failure, `NaN`, infinity, out-of-range scores and independent metric failure.
- Refusal tests verify the fixed actual answer, null generation metrics, no RAGAS call and correct aggregate refusal count and rate.
- Failure tests verify continued execution after one question failure, all-question failure returning a saved 200 report, and final persistence failure rolling back the entire version.
- Transaction tests verify that no result is committed before all questions finish and that an interrupted or failed final write permits the same evaluation version to run again.
- Dataset tests cover each state, active-only evaluation, archive idempotency, edit rejection after evaluation and replacement by a new row.
- Expected-chunk tests cover chunks from multiple documents, cross-knowledge-base IDs, old document versions, non-completed documents, deleted documents and all-or-nothing rejection.
- Reindex integration tests verify successful publication marks only overlapping active questions as `NEEDS_REVIEW`, failed rebuilding changes nothing, and state invalidation commits with version publication and old-chunk deletion.
- Feedback tests use two users and multiple sessions. They verify ownership filtering, assistant-role enforcement, nearest prior user-message pairing, unique overwrite and no client-controlled identity or question body.
- Candidate tests cover single-scope creation, feedback-source uniqueness, multi-scope no-candidate behavior, unknown historical scope, downvote/upvote archive and restore, and no automatic changes after formal-state promotion.
- Permission tests cover system administration, local `ADMIN`, denied `READ`, denied `WRITE`, public-read insufficiency, missing knowledge base and permission dependency failure.
- API tests cover duplicate evaluation version, no active questions, all-failed report, aggregate-only history, dataset state filtering, immutable evaluated rows and unpaginated chunk summaries without full content.
- Trace tests cover accepted safe client IDs, replacement of missing or unsafe IDs, response propagation, cleanup after response and isolation between concurrent requests.
- Observability tests assert low-cardinality categories and explicitly verify that questions, answers, expected answers, feedback comments, chunks, prompts and sensitive identifiers are absent from logs and metrics labels.
- Existing RAG query, Reranker, context trimmer, source builder, indexing, permission, chat-session and API dependency tests are prior art and required regression coverage.
- Tests use public behavior and external adapters. They must not add testing-only production branches, status bypasses or hidden configuration switches.
- Completion validation runs focused evaluation, shared-RAG, indexing, feedback, permission and Trace tests, then the full suite, Ruff and Mypy.

## Out of Scope

- New evaluation run, expected evidence or candidate database tables.
- Asynchronous evaluation workers, persistent progress, cancellation, pause, resume or run-level retry.
- Scheduled evaluation, automatic execution after document publication, CI gating, quality thresholds or release blocking.
- Evaluation-version-driven pipeline selection, runtime model switching or per-run parameter overrides.
- Persistent configuration snapshots for evaluation versions.
- Per-result retrieval latency, RAGAS latency, token count or cost fields.
- A maximum question count or overall synchronous run timeout.
- An application interaction for promoting candidates or review-state questions to `ACTIVE`.
- Per-question historical-result reads.
- Pagination for the chunk annotation list.
- Frontend evaluation, feedback or trace user interfaces.
- Automatic assignment of multi-knowledge-base feedback to one knowledge base.
- An explicit assistant-to-user-message parent ID.
- A persisted Reranker degradation flag or degradation-reason field.
- RAGAS evaluation for a Reranker-degraded question or an explicit refusal.
- Precision@K, Answer Correctness, semantic similarity or other unapproved metrics.
- Changes to online permission filtering, query rewriting, hybrid retrieval, RRF, Reranker fallback, confidence filtering, context trimming, citations or fixed refusal behavior.
- Using evaluation scores to block, rewrite, regenerate or retry an online answer.
- Public exposure of internal RAG execution diagnostics or formal evaluation metrics in the normal RAG response.
- A dedicated evaluation model, provider credential, embedding model or provider route.
- Duplicating online answers, references or complete chunk bodies in evaluation or feedback storage.

## Further Notes

- The detailed design and accepted architecture records remain the source for execution ordering and rationale; this tracker specification is the implementation contract.
- The deliberate synchronous-without-run-table choice means long requests have no durable progress or recovery. This is accepted for the first release and requires a future migration if evaluation becomes asynchronous.
- The deliberate chunk-ID-array choice means document rebuilding invalidates labels. The publication path is therefore responsible for marking affected active questions as needing review before old chunks are removed.
- The first release intentionally omits an application status-promotion interaction. Direct database promotion can bypass application validation, so operational instructions must include read-only validation before any update.
- The lack of an explicit turn parent ID accepts a concurrency ambiguity when multiple questions are submitted simultaneously within one conversation. Pairing uses session, creation time and message ID as agreed.
- Historical version comparison may involve different active dataset rows. Every report exposes metric-specific sample counts, and callers must not interpret unequal populations as a controlled experiment.
- The current environment resolves RAGAS 0.4.3 but cannot import it with the installed LangChain stack because a removed VertexAI compatibility module is referenced. Resolving and locking a runtime-compatible dependency set is the first implementation prerequisite.
- This Spec is fully decided, labeled `ready-for-agent`, and ready to be split into tracer-bullet implementation tickets.
