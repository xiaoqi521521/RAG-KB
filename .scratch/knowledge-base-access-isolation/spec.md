# 知识库级权限隔离

Type: spec
Status: ready-for-agent

## Problem Statement

企业知识库当前已经有知识库权限表、读写校验和检索范围过滤，但认证依赖仍返回固定管理员身份，权限规则尚未成为所有入口一致执行的边界。用户无法确信部门、用户、公开知识库和管理员权限在查询、文档管理与会话访问中得到相同处理。

尤其是，客户端只要知道其他人的 `session_id`，就可能复用该会话或读取其消息；这使对话会话成为绕过知识库隔离的内容通道。与此同时，所有检索入口必须保证无权范围在 Embedding、全文检索、向量检索、Reranker 和模型调用前被拒绝，而不是仅靠 Prompt 或前端选择器约束。

## Solution

采用参考资料的知识库级逻辑隔离。知识库是数据访问边界，`kb_id` 是文档与 chunk 查询的硬过滤范围；部门和用户是授权主体，不引入独立租户实体。

每个新请求通过 JWT 确认用户身份，再由身份提供者实时加载当前用户的部门、系统角色和启用状态。有效权限由系统管理员、知识库公开状态、用户授权和部门授权共同计算。路由层先阻断无权操作，检索仓储再只查询已授权知识库范围，形成用户可见的权限错误与数据层硬过滤两道防线。对话会话成为仅归属于创建用户的资源，所有复用、读取和保存均验证所有权。

## User Stories

1. As a 企业员工, I want to authenticate with a JWT, so that every知识库操作都有可追溯的当前用户。
2. As a 企业员工, I want my current department and role to be loaded for each new request, so that recent部门或角色调整能立即影响我的访问范围。
3. As a 企业员工, I want to read an公开知识库 without an individual grant, so that company-wide material can be shared conveniently.
4. As a 企业员工, I want an公开知识库 to grant only reading, so that public visibility never accidentally permits document changes.
5. As a 企业员工, I want my user-level and department-level grants to combine at the highest level, so that valid direct authorization is not weakened by my department membership.
6. As a 知识库管理员, I want a local `ADMIN` grant to apply only to its knowledge base, so that delegated management does not become global administration.
7. As a 系统管理员, I want to access every non-deleted knowledge base, so that I can perform platform-wide support and operations.
8. As a 知识库创建者, I want to receive a local `ADMIN` grant when creation succeeds, so that a newly created knowledge base always has a manager.
9. As a 部门成员, I want a knowledge base's department attribution not to automatically grant me write access, so that ownership metadata and authorization stay distinct.
10. As a 知识库用户, I want to query only knowledge bases for which I have read access, so that answers never include restricted reference content.
11. As a 知识库用户, I want a multi-knowledge-base query to fail as a whole when one requested range is unauthorized, so that the answer never silently omits part of my requested scope.
12. As a 知识库用户, I want a permission failure to happen before model processing starts, so that I receive a clear error without consuming model quota.
13. As a 知识库用户, I want document lists, indexing status and downloads protected by read access, so that non-mutating information is not exposed across knowledge bases.
14. As a 知识库管理员, I want uploads, replacements, deletion and reindexing protected by write access, so that readers cannot alter the corpus.
15. As a 知识库用户, I want a document ID nested under the wrong knowledge base ID to appear absent, so that path manipulation cannot expose another knowledge base's document.
16. As a 知识库用户, I want all retrieval channels to apply the same knowledge base range, so that full-text search cannot bypass vector-search protection.
17. As a 知识库用户, I want only completed, current and non-deleted documents to enter retrieval, so that incomplete, stale or deleted content cannot be surfaced.
18. As a 知识库用户, I want reranking, context trimming, citation generation and answer generation to consume only authorized retrieval results, so that downstream RAG steps cannot expand my access.
19. As a 对话会话 owner, I want only myself to resume my session, so that an exposed session identifier does not grant access to my conversation.
20. As a 对话会话 owner, I want only myself to list my session messages, so that another user cannot inspect my prior questions, answers or 引用来源.
21. As a 对话会话 owner, I want message persistence to verify ownership too, so that non-HTTP or asynchronous execution paths cannot write into another user's session.
22. As a 系统管理员, I want my global knowledge base authority not to implicitly reveal other users' chat history, so that support access does not accidentally become conversation surveillance.
23. As a 对话会话 owner, I want to keep my historical answers after a later knowledge base permission revocation, so that current scope remains implementable without retroactive conversation rewriting.
24. As a 知识库用户, I want every new RAG query to be authorized in real time even when I can still see old history, so that current document access is never granted by a past conversation.
25. As a security operator, I want invalid or expired JWTs rejected before business processing, so that unauthenticated traffic cannot reach knowledge base resources.
26. As a security operator, I want missing or deleted resources to return a distinct not-found result, so that clients can correct invalid identifiers.
27. As a security operator, I want existing resources without effective permission to return forbidden, so that clients can distinguish authorization failure from an invalid request.
28. As a security operator, I want an unavailable identity or permission dependency to fail closed, so that an outage never becomes an authorization bypass.
29. As a platform operator, I want authorization results to be computed without cross-request caching, so that grant, role and department changes apply to the next request.
30. As a platform operator, I want permission denials and latency observable without logging document content or tokens, so that security behavior can be monitored safely.
31. As a developer, I want one permission service to define effective permission semantics, so that routes do not independently reinterpret public, user and department grants.
32. As a developer, I want repositories to require an authorized knowledge base range for retrieval, so that data access remains protected when orchestration evolves.
33. As a developer, I want ownership-aware session repository operations, so that callers cannot accidentally omit session user filtering.
34. As a developer, I want permission behavior tested at the HTTP boundary, so that authentication, route checks and downstream short-circuiting are verified together.
35. As a developer, I want retrieval and session ownership queries tested directly at the repository boundary, so that the non-bypassable SQL constraints remain stable.
36. As a product owner, I want authorization management APIs deferred, so that this work delivers access enforcement without silently expanding into membership administration.

## Implementation Decisions

- The isolation model is knowledge-base scoped. `kb_id` is the mandatory data boundary; `department_id` describes an authorization subject attribute, not a tenant identifier.
- The system does not introduce an independent tenant entity, tenant columns, organization-wide cross-tenant administration, or row-level-security replacement in this scope.
- JWT authentication identifies the user. A separate identity provider loads the current user's department, system role and enabled state for each request before creating the request-scoped 当前用户.
- Authentication, user-directory management, local passwords and concrete enterprise SSO/LDAP integration are separate responsibilities. This feature only defines the identity-provider boundary it consumes.
- The request context carries identity for logging and service coordination only. It must not carry a trusted cached knowledge base list or replace repository query constraints.
- Effective permission uses the ordered levels `READ < WRITE < ADMIN`. A system administrator is globally `ADMIN`; otherwise, a public knowledge base provides at least `READ`, and user plus department grants resolve to their highest level. There is no explicit deny permission.
- A local `ADMIN` grant permits management only for the associated knowledge base. It is not a system administrator role.
- Creating a knowledge base and granting its creator local `ADMIN` are one atomic unit of work. Department attribution does not create a department write grant.
- Read operations are knowledge base listing, RAG retrieval, document listing, indexing status and original-file download. Write operations are upload, content replacement, deletion and reindexing. Write requires effective `WRITE` or `ADMIN`.
- Authorization management is intentionally excluded. Existing grants are consumed but no members, grant, revoke or permission-change API is added.
- Every knowledge-base-scoped route checks required permission before invoking document services, indexing, Embedding, retrieval, reranking or answer generation.
- For a request containing multiple knowledge bases, normalize and de-duplicate IDs while preserving order, then require read access to every ID. Any denied ID rejects the entire request; the server must not silently filter the request to a subset.
- Retrieval receives only the fully authorized knowledge base range. Vector and full-text queries both filter by that range, document current version, completed status and non-deleted status. All later RAG stages consume only this result set.
- Document operations validate that the requested document belongs to the path's knowledge base. A missing, deleted or mismatched document is treated as not found.
- Background indexing receives document identity from an already-authorized submission and derives its knowledge base identity from the document record. It never trusts a client-provided arbitrary knowledge base identity for chunk ownership.
- A 对话会话 belongs to one user. Session reuse, activity updates, message reads and turn persistence require both the session identifier and owner user identifier, and exclude deleted sessions. Ownership mismatch is not found.
- System administrator authority does not bypass conversation ownership. Cross-user conversation audit access requires a separate future design.
- Knowledge base permission revocation does not retrospectively hide or rewrite existing private 对话历史. It never authorizes a new retrieval request.
- Do not cache user profiles, department membership or effective permissions across requests. A later performance optimization may batch reads within one request but must preserve the same fail-closed semantics.
- Invalid or missing authentication returns `401`; missing, deleted or ownership-mismatched resources return `404`; known resources without effective permission return `403`; identity or permission dependency unavailability returns `503`; unexpected errors return a safe `500` response.
- Identity and permission dependency failures fail closed. No downstream storage, retrieval or model call starts when access cannot be determined.
- Record authorization outcome, permission source, action and latency through structured logs and metrics. Do not log JWTs, full questions, documents, chunks, chat messages, prompts, secrets or high-cardinality user and resource identifiers in metric labels.
- The existing persistence model is sufficient for knowledge base, permission, document, chunk and session ownership. This spec does not require a database migration; any future local user-directory schema is owned by authentication work.

## Testing Decisions

- The principal test seam is the public FastAPI route boundary. Tests override authentication, permission and downstream RAG dependencies, then assert response status and externally observable short-circuit behavior rather than private method calls.
- Route-level tests cover invalid authentication, public and granted reads, denied reads and writes, creation-time local administration, document read/write matrix, and whole-request rejection when one requested knowledge base is unauthorized.
- Route-level RAG tests prove that a forbidden range reaches neither Embedding, retrieval nor model generation. Successful multi-range calls pass the exact normalized authorized range to downstream query services.
- Repository-level tests are reserved for two non-bypassable boundaries: retrieval statements include knowledge base, current-version, completed and non-deleted conditions; session and message reads require both session identity and owner user identity.
- Session tests use two distinct users and prove that reuse, history reads, activity updates and turn persistence cannot cross ownership boundaries, including for a system administrator.
- Permission-service tests cover system administration, public read-only access, user-plus-department highest-level resolution, absent grants, deleted knowledge bases and each required HTTP error category.
- Document-resource tests cover a valid document nested under its own knowledge base and an existing document requested through another knowledge base path, which must be not found.
- Tests validate behavior at public seams using fakes only for external boundaries such as JWT verification, identity directory, database session or model provider. They must not add testing-only branches or mock internal private implementation.
- Existing API dependency-override tests and retrieval SQL statement tests are the prior art for this feature. Preserve existing RAG, document and chat tests as regression coverage.
- Completion validation runs the focused permission, RAG, knowledge-base, chat-session and chunk-repository tests, then the full test suite and configured lint and type checks.

## Out of Scope

- Independent tenant modeling, tenant data migration and organization-level isolation policies.
- User registration, password storage, login UX, token issuance UI, SSO/LDAP implementation and user-directory administration.
- Knowledge base membership listing, permission grant, revoke or change endpoints.
- Explicit deny rules, time-limited grants, nested departments or delegated department administration.
- Permission caching, cache invalidation, Redis authorization storage or permission event propagation.
- Retroactive hiding, deletion, redaction or re-authorization of existing 对话历史 after permission changes.
- Cross-user chat audit and support-read capability.
- PostgreSQL Row Level Security, external policy engines and database-permission redesign.
- RAG algorithm changes, including query rewriting, hybrid retrieval ranking, Reranker behavior, context trimming, citation logic and answer-quality evaluation.
- Frontend authorization UI, document-member management UI and security administration dashboards.

## Further Notes

- This specification follows the established domain language of 知识库级逻辑隔离, 授权主体, 当前用户, 有效权限, 知识库范围拒绝, 读操作 and 写操作.
- It implements the reference project's access-control model while correcting the current missing session-ownership boundary.
- The architecture decision to use knowledge-base-scoped isolation rather than an independent tenant model is recorded separately to prevent accidental future drift.
- The detailed design document remains the source for execution order, observability names and module-level rationale. This tracker specification is ready to be split into implementation tickets.
