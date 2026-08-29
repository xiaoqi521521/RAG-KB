# 19-多租户权限隔离 Plan

本文基于 [19-多租户权限隔离.md](../references/19-多租户权限隔离.md)、[05-数据库设计.md](../references/05-数据库设计.md) 与本次设计访谈形成。它描述 LangChain / FastAPI 重构项目中的知识库级逻辑隔离方案，并同步记录已落地的检索层权限过滤实现。

## 1. 目标与边界

参考资料所称的“多租户”并没有独立 `tenant_id` 或租户实体。其实际模型是以知识库为访问边界，通过公开状态、用户授权、部门授权和管理员身份控制数据可见性。本项目严格沿用该模型：`kb_id` 是文档、chunk 和检索的强制范围，`department_id` 是授权主体属性，而不是租户 ID。

本阶段目标是使认证、授权、知识库资源访问、检索和会话访问遵循同一套边界。权限必须在模型调用之前完成，并由仓储 SQL 再次限制数据范围，不能依赖 Prompt 或前端传参；`ContextVar` 只传递当前用户身份，不能直接充当授权范围。

相关架构决策见 [ADR-0003](../adr/0003-use-knowledge-base-scoped-logical-isolation.md)，术语见 [CONTEXT.md](../../CONTEXT.md)。

### 1.1 In Scope

- 使用 JWT 身份验证和 FastAPI 依赖建立 `CurrentUser`。
- 从身份提供者实时加载用户部门、系统角色和启用状态。
- 使用公开知识库、用户授权、部门授权和系统管理员角色计算有效权限。
- 对读、写、查询、文档资源和会话资源执行硬权限校验。
- 对向量和全文检索使用相同的 `kb_id` SQL 范围过滤。
- 补齐会话归属校验，防止已知 `session_id` 被跨用户读取或写入。
- 增加权限拒绝、资源范围与检索短路的测试、日志和指标。

### 1.2 Out of Scope

- 不新增独立 `tenant_id`、租户表或组织级跨租户管理。
- 不新增用户注册、密码登录、SSO/LDAP 具体适配或用户目录管理。
- 不新增知识库成员列表、授权授予、变更或回收 API。
- 不缓存用户资料、部门归属或权限结果。
- 不在本阶段因权限回收追溯隐藏、删除或重算既有历史会话。
- 不用 PostgreSQL Row Level Security 替代应用层的显式授权范围；后续如引入，应单独设计与连接池上下文的关系。
- 不修改 RRF、Reranker、Token 裁剪、引用或生成算法。

## 2. 已确认决策

| 主题 | 方案 |
| --- | --- |
| 隔离边界 | 知识库级逻辑隔离，`kb_id` 是强制数据范围，不新增 `tenant_id`。 |
| 身份来源 | JWT 只确认用户 ID；每个新请求由身份提供者加载部门、系统角色和启用状态。 |
| 系统管理员 | 用户资料中的系统管理员可读写所有知识库。 |
| 局部管理员 | `kb_permission.permission=ADMIN` 只对对应 `kb_id` 有效，不能跨知识库。 |
| 授权合并 | 公开知识库至少提供 `READ`；用户和部门授权并存时取 `READ < WRITE < ADMIN` 中最高等级；首期没有 `DENY`。 |
| 多库请求 | 请求的任一 `kb_id` 无读权限即整体 `403`，不静默缩小范围，也不启动 Embedding、检索或模型调用。 |
| 文档操作 | 列表、状态、下载和问答为读操作；上传、替换、删除与重建索引为写操作。 |
| 创建知识库 | 在同一工作单元创建知识库并向创建者写入用户级 `ADMIN` 授权；归属部门不自动取得写权限。 |
| 会话 | 会话归单个用户所有。复用、读历史和保存消息都按 `session_id + user_id + is_deleted=false` 查询；系统管理员不默认绕过。 |
| 历史会话 | 权限回收不追溯既有私人历史会话；新检索请求仍实时授权。 |
| 缓存 | 不缓存身份资料或授权结果。 |
| 授权依赖故障 | 身份提供者或权限数据不可用时返回 `503` 并停止处理；不得故障放行。 |

## 3. 领域模型与数据约束

### 3.1 身份与授权主体

`CurrentUser` 是请求内身份快照，至少包含 `user_id`、`department_id` 和 `role`。它由认证依赖设置到 `ContextVar`，只为日志、异步上下文传递和服务协作提供便利，不能作为仓储隐式权限来源。

身份提供者是认证模块之外的边界。它在 JWT 验证成功后按 `user_id` 返回当前部门、系统角色和启用状态；首期可以是本地用户目录，生产可由企业 SSO/LDAP 适配。该模块不设计登录、密码或用户表。

`kb_permission` 保持参考资料的唯一约束：每个 `(kb_id, subject_type, subject_id)` 最多一条记录。`subject_type` 为 `USER` 或 `DEPARTMENT`，`permission` 为 `READ`、`WRITE` 或 `ADMIN`。

### 3.2 有效权限

对非系统管理员，`PermissionService` 在知识库未删除的前提下按如下顺序计算：

```plain
系统管理员
  -> ADMIN
非系统管理员且知识库公开
  -> 至少 READ
用户授权 + 部门授权
  -> 取最高等级
无公开状态且无任一授权
  -> 无权限
```

公开状态不会授予写权限。个人授权不能降低部门的更高权限，因为首期无显式拒绝规则；需要例外时调整部门成员关系或回收部门授权。

`PermissionService` 是授权规则的唯一实现点，提供至少：

```plain
require_read(kb_id, user)
require_write(kb_id, user)
filter_readable_kb_ids(kb_ids, user)
get_highest_permission(kb_id, user)
```

知识库不存在或已软删除时不再计算权限。

## 4. 请求与数据流

### 4.1 认证与请求上下文

```plain
Authorization: Bearer <JWT>
  -> 验证签名、过期时间和用户标识
  -> IdentityProvider.load(user_id)
  -> 拒绝停用或不存在的用户
  -> 设置 CurrentUser ContextVar
  -> 路由与服务执行
  -> finally reset ContextVar token
```

认证失败返回 `401`。用户资料加载或权限数据访问发生不可用错误时返回 `503`，不将异常转换为有权限或无权限，也不继续访问文档、检索或模型。

### 4.2 知识库访问矩阵

| 操作 | 所需有效权限 |
| --- | --- |
| 知识库列表 | 只返回当前用户可读的知识库；系统管理员返回全部未删除知识库。 |
| 问答、检索 | `READ` 及以上；请求范围中任一知识库失败即整体拒绝。 |
| 文档列表、状态、原文件下载 | `READ` 及以上。 |
| 上传、内容替换、删除、重建索引 | `WRITE` 及以上。 |
| 创建知识库 | 已认证用户；创建者自动获得该库 `ADMIN`。 |
| 授权管理 | 不在本阶段提供 API。 |

路由层必须在调用业务服务前执行 `require_read` 或 `require_write`。这给调用方稳定的错误语义，也保证无权请求不会创建会话、写任务、调用 Embedding 或产生模型成本。

### 4.3 资源嵌套范围

仅校验路径中的 `kb_id` 不足以保护文档资源。所有文档状态、下载、删除、内容替换和重建操作都必须确认 `document.kb_id == path.kb_id`，不匹配时按资源不存在处理。`KnowledgeBaseService._get_document_in_kb(...)` 已体现这一模式，后续仓储查询应尽量收敛为带 `kb_id` 的方法，减少先查后比对的遗漏风险。

索引后台任务不携带用户上下文。它只能处理由已授权入口创建的文档任务，并从文档记录读取 `kb_id` 写入所有 chunk；后台任务不能接收客户端提供的任意 `kb_id` 作为数据归属。

### 4.4 检索硬过滤

查询入口先对规范化、去重后的每个 `kb_id` 调用 `require_read`。校验全部成功后，才将这一范围传入查询服务和检索仓储。

混合检索入口还必须执行第二层检索范围过滤。`HybridRetriever.retrieve(...)` 从当前请求 `ContextVar` 读取用户，复用 `PermissionService.filter_readable_kb_ids(...)` 逐库计算 `allowed_kb_ids`，并在执行 Embedding、向量检索和全文检索前移除无权、不存在或已删除的知识库；内部 `_retrieve(...)` 只接受已过滤范围。部分范围被过滤时继续查询剩余有权范围；全部被过滤时返回 `403`。权限数据源不可用返回 `503`，缺少当前用户上下文返回 `401`。v2/v3/v4 使用该受保护入口，v3/v4 的 HyDE 向量检索复用同一 `allowed_kb_ids`；v1 直接向量管道不新增该 HybridRetriever 内部过滤。

向量与全文检索必须使用同一范围和文档可用条件：

```sql
WHERE kb_doc_chunk.kb_id IN (:allowed_kb_ids)
  AND kb_doc_chunk.doc_version = kb_document.version
  AND kb_document.status = 'DONE'
  AND kb_document.is_deleted = FALSE
```

全文检索只是在上述范围上附加 `content_tsv @@ to_tsquery(...)`；RRF、Reranker、上下文裁剪、引用构建和模型生成只能消费这个受限结果集，不能重新查询或扩大 `kb_ids`。`ContextVar`、Prompt 和前端选择器均不能替代这条 SQL 约束。

当前 `ChunkRepository.search_by_vector(...)` 和 `search_by_fulltext(...)` 已包含知识库、版本、状态和删除过滤。混合检索的原始方法只接收已经过滤的 `allowed_kb_ids`，不读取用户上下文；受保护入口负责计算范围。所有混合检索下游方法均复用同一约束，不提供未过滤的业务入口。

### 4.5 会话隔离

会话列表已经按 `user_id` 过滤，但单会话和消息访问也必须补齐归属条件。设计的仓储接口应表达所有权，而不是让调用方先读后判断：

```plain
get_active_session_for_user(session_id, user_id)
touch_owned_session(session_id, user_id)
list_messages_for_user(session_id, user_id)
add_turn_for_user(session_id, user_id, ...)
```

复用不存在、已删除或不属于当前用户的会话一律返回 `404`。保存消息前同样验证会话所有权，避免通过异步或服务层路径绕过 HTTP 路由。系统管理员默认不读取其他用户会话；审计读取需要未来独立设计。

历史会话仅按用户归属保护。用户随后失去某个知识库权限时，当前阶段仍可看到自己已经保存的历史回答；但其发起的任何新问答必须重新通过全部知识库读权限检查。

## 5. 错误、日志与可观测性

| 条件 | 状态 | 行为 |
| --- | --- | --- |
| JWT 缺失、无效或过期 | `401` | 不建立 `CurrentUser`，不进入业务服务。 |
| 知识库、文档或归属会话不存在或已删除 | `404` | 不进行检索或资源操作。 |
| 资源存在但当前用户权限不足 | `403` | 记录最小化拒绝日志，不调用下游。 |
| 身份提供者、权限查询或其数据库不可用 | `503` | 拒绝式失败，不调用下游。 |
| 其他未预期异常 | `500` | 由统一异常处理器返回通用错误，不泄露内部实现。 |

检索层权限过滤的专用审计日志可记录当前 `user_id`、被过滤的 `denied_kb_ids`、操作类型、过滤结果、`permission_source=permission_service`、数量、耗时和错误类型；逐库授权的系统管理员、公开、用户或部门来源仍由无标识的通用权限观测记录。其他权限与认证日志不得记录用户 ID 或知识库 ID。所有日志仍不得记录 JWT、完整问题、chunk 正文、文件内容、完整会话消息、文档 ID、对象路径或授权密钥。上述标识不得作为 Prometheus/Grafana 指标标签。

建议新增计数与耗时指标：

```plain
rag_permission_checks_total{action,result,source}
rag_permission_check_duration_seconds{action}
rag_permission_denials_total{action,reason}
```

指标标签不得包含 `user_id`、`kb_id` 或文档 ID，避免高基数和敏感标识泄露。

## 6. 实施影响

### 6.1 计划调整的模块

```plain
app/api/dependencies.py
  -> 用 JWT 验证和 IdentityProvider 替换固定 ADMIN 占位用户；finally 中清理 ContextVar。

app/core/security.py
  -> 集中 JWT 的签发或验证辅助逻辑；不承载权限计算。

app/core/context.py
  -> 保持请求内 CurrentUser 表达，不把授权范围写入上下文。

app/services/permissions.py
  -> 保持有效权限计算、读写检查和明确错误边界；提供 `filter_readable_kb_ids(...)` 供混合检索逐库过滤。

app/services/hybrid_retriever.py
  -> 在 `retrieve(...)` 入口读取当前用户并过滤检索范围；`_retrieve(...)` 只处理已授权范围。

app/services/enhanced_retriever.py
  -> 将 `HybridRetrieveResult.allowed_kb_ids` 传给 HyDE 向量检索。

app/api/routes/rag.py
  -> 将请求内的 `PermissionService` 注入 v2/v3/v4 混合检索依赖。

app/repositories/permissions.py
  -> 提供用户、部门授权的批量或单项读取；不在此层实现角色规则。

app/repositories/chunks.py
  -> 所有检索方法继续强制接收已授权 kb 范围，并保留版本、状态、删除过滤。

app/repositories/chat.py
app/services/chat_sessions.py
app/api/routes/chat.py
  -> 将会话复用、消息读取和保存改为按用户归属的仓储操作。

app/services/knowledge_base.py
  -> 保持创建知识库和创建者 ADMIN 授权处于同一工作单元；文档操作持续校验资源归属。
```

首期不要求数据库迁移：`kb_knowledge_base`、`kb_permission`、`kb_document`、`kb_doc_chunk` 和 `kb_chat_session.user_id` 已覆盖该方案。若身份目录由本地数据库实现，其用户表属于认证模块独立设计，不能隐式塞入本模块迁移。

### 6.2 性能约束

首期每个请求实时读取身份和授权，不使用 Redis 缓存。对于多知识库请求，实施时可将逐库授权查询优化为批量读取，但优化后的语义必须保持“全部请求范围均获准才继续”，并且不能把授权结果跨请求缓存。

## 7. 测试计划

测试优先通过 FastAPI 路由、服务公开方法和实际 SQLAlchemy statement 验证行为，不为测试添加生产绕过分支。

### 7.1 权限服务

- 系统管理员可读写任意未删除知识库。
- 公开知识库对普通用户仅提供读权限。
- 用户和部门授权取最高等级。
- 无授权时读写均为 `403`。
- 不存在或已删除知识库为 `404`。
- 身份或权限依赖异常映射为 `503`，且不放行。

### 7.2 API 与检索短路

- 未认证请求为 `401`。
- 一个请求包含重复 `kb_id` 时只校验一次且保持顺序。
- 多知识库请求任一未授权时返回 `403`，Embedding、检索和模型服务均未被调用。
- route 全部授权时，混合检索的向量和全文检索均收到相同的 `allowed_kb_ids`；检索层部分过滤时两路也必须使用完全相同的过滤范围。
- 混合检索被直接调用时，部分无权知识库会被过滤；全部无权返回 `403`，无用户上下文返回 `401`，权限数据源失败返回 `503`。
- v3/v4 的 HyDE 向量检索必须复用混合检索返回的 `allowed_kb_ids`，不能重新使用原始请求范围。
- 向量、全文 SQL 均包含 `kb_id`、当前版本、`DONE` 和未删除过滤。

### 7.3 文档与会话资源

- 路径 `kb_id` 与文档实际归属不一致时返回 `404`。
- `READ` 用户可以查看列表、状态和下载，不能上传、删除、替换或重建。
- `WRITE` 和局部 `ADMIN` 用户可执行文档管理操作。
- 创建知识库后创建者具备该库局部 `ADMIN` 授权。
- 用户无法复用、读取、保存或更新另一用户的会话；这些情况均为 `404`。
- 系统管理员默认同样不能访问其他用户会话。
- 失去知识库读权限后，用户不能发起该知识库的新检索，但仍能读取自己的既有会话。

## 8. 推荐实施顺序

1. 为 JWT 验证和身份提供者定义最小依赖接口，替换固定管理员占位。
2. 补充统一业务异常映射和权限依赖故障的 `503` 分支。
3. 固化 `PermissionService` 的授权合并与读写测试，必要时批量化查询但不改变语义。
4. 审查所有知识库、文档和查询路由，确保入口权限校验在任何下游调用之前。
5. 审查并收敛所有 chunk 检索 SQL 的 `kb_id`、版本、状态和软删除条件。
6. 修复会话仓储、服务和路由的用户归属校验。
7. 接入最小化日志和 Prometheus 指标。
8. 运行权限、检索、文档、会话相关测试及全量质量检查。

## 9. 验收标准

- 无权用户不能通过普通问答、流式问答、文档 API、检索 SQL、会话 ID 或 Prompt 获得其他知识库内容。
- 权限失败在 Embedding、检索、Reranker、模型调用和索引任务创建之前结束。
- 两路检索及后续 RAG 管道只处理已授权知识库产生的 chunk。
- 用户、部门、公开状态、局部管理员和系统管理员的权限语义在所有入口一致。
- 文档嵌套资源和会话资源均有仓储级归属过滤。
- 权限或身份依赖失败不会放行访问；日志与指标不暴露内容或高基数敏感标签。
- 本阶段不引入独立租户模型、权限缓存或授权管理 API。

## 10. 验证命令

实施完成后至少执行：

```powershell
uv run pytest tests/services/test_permissions.py -v
uv run pytest tests/api/test_rag.py tests/api/test_knowledge_bases.py -v
uv run pytest tests/services/test_chat_sessions.py tests/api/test_chat.py -v
uv run pytest tests/repositories/test_chunks.py -v
uv run pytest -v
uv run ruff check .
uv run mypy app
```

检索层权限过滤已完成实现。已执行 `uv run pytest -q`，结果为 `383 passed, 3 skipped`，并通过 `uv run ruff check .`；`uv run mypy app` 的剩余错误来自既有未修改模块，实施代码未新增类型错误。
