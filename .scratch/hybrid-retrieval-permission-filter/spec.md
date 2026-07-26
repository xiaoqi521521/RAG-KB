# 混合检索层权限范围过滤

Type: spec
Status: ready-for-agent

## Problem Statement

当前 route 层会在问答请求进入缓存、会话、Embedding 和检索之前校验完整的知识库范围，但 `HybridRetriever` 只信任调用方传入的 `kb_ids`。如果混合检索被其他内部入口直接调用，检索服务本身不会根据当前用户权限过滤范围。

参考项目在混合检索服务中计算 `allowedKbIds` 后再查询。当前项目需要引入同等粒度的检索层防护，同时保留现有 route 的整体拒绝语义：API 请求中任一知识库无权仍整体返回 `403`，检索层独立调用时则过滤无权范围并查询剩余有权知识库。

## Solution

为 `HybridRetriever` 增加受保护的业务入口。该入口从当前请求上下文读取 `CurrentUser`，复用现有 `PermissionService` 逐个计算知识库读权限，规范化并过滤出 `allowed_kb_ids`，再执行向量检索、全文检索和 RRF。

原始混合检索只接收已经过滤的授权范围，并作为受保护入口的内部实现。`HybridRetrieveResult` 向增强检索暴露实际使用的授权范围，使 v3/v4 的 HyDE 向量检索使用完全相同的 `allowed_kb_ids`，避免 HyDE 旁路重新使用原始请求范围。

该设计是现有知识库级权限隔离的第二道防护，不改变 route 层的认证、资源错误和多知识库整体拒绝契约。

## User Stories

1. As a 知识库用户, I want the hybrid retriever to apply my current read permissions before retrieval, so that an internal retrieval call cannot search a knowledge base I cannot read.
2. As a 知识库用户, I want a query over partially readable knowledge bases to search only the readable subset inside the retrieval layer, so that authorized content remains useful without exposing restricted chunks.
3. As a 知识库用户, I want the HTTP route to continue rejecting a multi-knowledge-base request when any requested knowledge base is unauthorized, so that the API does not silently change the requested scope.
4. As a 知识库用户, I want the hybrid retriever to use the same filtered range for vector retrieval and full-text retrieval, so that one retrieval channel cannot bypass the other channel's boundary.
5. As a v3/v4 RAG user, I want HyDE vector retrieval to reuse the range filtered by the original hybrid retrieval, so that model-generated hypothetical answers cannot widen access.
6. As a knowledge-base operator, I want a request with no readable knowledge bases to fail as forbidden before Embedding or retrieval starts, so that an authorization failure is not confused with an empty knowledge result.
7. As a knowledge-base operator, I want a missing or deleted knowledge base to be excluded from an independent retrieval scope, so that stale identifiers cannot enter chunk retrieval.
8. As a knowledge-base operator, I want permission-store failures to return service unavailable rather than look like denials, so that an outage is distinguishable from a real permission decision.
9. As a security operator, I want a protected retrieval call without an initialized current-user context to return unauthorized, so that missing authentication cannot fall through to a default identity.
10. As a security operator, I want system-admin, public, user-grant and department-grant behavior to remain consistent with the existing permission service, so that retrieval does not create a second authorization model.
11. As a security operator, I want permission changes to affect the next retrieval call without a cross-request permission cache, so that revoked access is not retained by stale authorization state.
12. As a developer, I want the protected hybrid-retrieval entry to normalize and de-duplicate knowledge-base IDs while preserving first appearance order, so that repeated IDs do not cause repeated checks or unstable downstream scope.
13. As a developer, I want the raw hybrid retrieval implementation to accept only an already filtered scope, so that business callers cannot accidentally bypass the permission-aware entry.
14. As a developer, I want one existing permission service to define effective read permission, so that public visibility and user/department grant levels are not reimplemented in the retriever.
15. As a developer, I want the actual filtered scope returned with hybrid retrieval results, so that downstream enhancements can preserve the same security boundary without reading request state again.
16. As a platform operator, I want the retrieval permission filter to check each requested knowledge base independently, so that its partial-filter behavior matches the reference design while keeping the existing authorization rules.
17. As a platform operator, I want a dedicated permission-filter audit event to include the current user and filtered knowledge-base IDs, so that an operator can investigate why a retrieval scope was narrowed.
18. As a platform operator, I want those audit identifiers excluded from metric labels, so that troubleshooting logs do not create high-cardinality monitoring dimensions.
19. As a test engineer, I want retrieval tests to prove unauthorized IDs never reach vector or full-text repositories, so that the security boundary is verified at the highest useful seam.
20. As a test engineer, I want HyDE tests to prove its vector repository receives the same authorized scope as the original hybrid search, so that secondary retrieval paths cannot regress into an unfiltered query.
21. As a maintainer, I want v1's direct vector pipeline to remain explicitly out of this HybridRetriever-specific change, so that this Spec does not silently expand into a shared authorization refactor.

## Implementation Decisions

- This Spec extends the existing knowledge-base access-isolation baseline. It changes the internal hybrid-retrieval boundary, not the public route authorization contract.
- The protected hybrid-retrieval entry is the highest new test seam. It reads the current user from the request `ContextVar`, then passes that user explicitly to the existing `PermissionService`.
- If the current-user context is missing, the protected entry fails with `401`. It must never use a default user, a default administrator or an empty identity.
- `PermissionService` remains the single source of effective permission rules. The retriever does not directly reinterpret system-admin, public, user-grant, department-grant or permission-level semantics.
- Filtering checks each requested knowledge base independently and does not cache authorization results across requests. An implementation may batch database reads later only if it preserves the same per-knowledge-base behavior and error semantics.
- Input knowledge-base IDs are de-duplicated while preserving first appearance order before permission filtering.
- A knowledge base that is missing or deleted is excluded from the retrieval authorization scope. If no scope remains, the protected entry returns `403`.
- A permission or knowledge-base data-source failure returns `503` and stops downstream processing. It must not be converted into a denial or an empty retrieval result.
- If some IDs are readable and some are not, the protected retrieval entry continues using only the readable `allowed_kb_ids`. The removed IDs are included in the dedicated audit event.
- If no ID is readable, the protected entry returns `403` before Embedding, vector retrieval, full-text retrieval or model calls.
- The raw hybrid retrieval implementation is internal and receives only `allowed_kb_ids`. It performs Embedding, vector retrieval, full-text retrieval and RRF within that scope.
- The hybrid retrieval result includes the actual `allowed_kb_ids` used for the retrieval. This is an internal result field for downstream scope propagation, not a new public API response field.
- v2, v3 and v4 use the protected hybrid-retrieval entry. v3 and v4 pass the returned authorized scope into HyDE vector retrieval. RRF, Reranker, context trimming, citation construction and generation consume only results from that scope.
- The v1 direct vector pipeline is out of scope for this HybridRetriever-specific permission filter. Its current route-level authorization and SQL range filtering remain unchanged.
- Route behavior remains all-or-nothing for multi-knowledge-base requests: any unauthorized requested ID returns `403` before cache, session, Embedding, retrieval or model processing.
- The dedicated retrieval permission audit event may record `user_id` and `denied_kb_ids`, together with operation, result, permission source, counts, duration and error type. Other permission/authentication logs retain the existing prohibition on user and knowledge-base identifiers.
- User and denied knowledge-base identifiers must not be used as Prometheus or Grafana metric labels. Logs must not contain JWTs, questions, answers, document content, document IDs, object paths or session messages.
- No database migration, new permission table, permission cache, authorization API or frontend change is part of this Spec.

## Testing Decisions

- The primary seam is the protected public entry of `HybridRetriever`. Tests should assert externally observable scope propagation and short-circuit behavior, not private helper call order.
- Existing route tests remain regression coverage for whole-request rejection, duplicate normalization, no session creation, no cache access and no downstream RAG call after a denial.
- Hybrid retriever tests cover all-readable input, partially readable input, all-filtered input, duplicate and ordered IDs, public knowledge bases, grant levels, system administrators, missing/deleted knowledge bases, missing current-user context and permission-store failure.
- Hybrid retriever tests assert that vector and full-text repository calls receive exactly the same filtered scope and that no Embedding occurs when the scope is empty.
- Enhanced retriever tests cover HyDE scope propagation: the original hybrid result's authorized range must be the exact range passed to the HyDE vector repository.
- Existing repository statement tests remain responsible for verifying `kb_id`, current document version, completed status and non-deleted filters. This Spec does not replace those SQL boundary tests.
- API tests retain the existing route seam and prove that route-level all-or-nothing behavior remains unchanged even though the internal retriever supports partial filtering when independently called.
- Audit-log tests verify that the dedicated permission-filter event carries `user_id` and `denied_kb_ids`, while metric attributes and unrelated logs do not carry these identifiers.
- Tests should use the existing fakes and dependency overrides for identity, permission data, repositories and model providers. No test-only production bypass or default-admin branch may be introduced.
- Validation includes the focused permission, hybrid-retriever, enhanced-retriever, RAG route and chunk-repository tests, followed by the full test suite and configured lint/type checks.

## Out of Scope

- Changing the route layer from whole-request rejection to partial success.
- Adding the same internal permission filter to v1 or creating a new shared authorization component for every retrieval pipeline.
- Reimplementing effective permission rules inside `HybridRetriever` or `ChunkRepository`.
- Permission caching, Redis authorization state, grant invalidation events or permission-management APIs.
- Changes to vector similarity, PostgreSQL full-text query construction, RRF ranking, HyDE generation, Reranker behavior, context trimming, citations or answer generation.
- New tenant entities, tenant columns, organization administration or PostgreSQL Row Level Security.
- User-directory, JWT issuance, SSO/LDAP integration or frontend authorization UI.
- Retroactive filtering or rewriting of existing conversation history.

## Further Notes

- The detailed design and rationale are recorded in the retrieval-layer design output and ADR-0007. The scoped audit-log exception is recorded in ADR-0008.
- This Spec deliberately distinguishes the route's `知识库范围拒绝` from the retrieval layer's `检索授权范围`; the former protects the caller's requested scope, while the latter protects the data actually queried by the hybrid retriever.
- This Spec is ready for implementation and should be split into tracer-bullet tickets only after the implementation agent confirms the current dependency assembly can construct the existing permission service for the retriever's database session.
