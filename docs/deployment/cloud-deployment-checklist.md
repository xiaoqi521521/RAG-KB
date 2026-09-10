# 云服务器部署改造清单

> 生成时间：2026-08-29
> 基于对当前代码库的审查结果。

## 结论

当前项目是「可测试的单机原型」，不能直接暴露公网。核心 RAG、权限过滤、降级逻辑已有基础，但生产部署链路尚未闭环。

## P0：上线前必须完成

### 1. 替换演示认证

- **证据**：`app/services/identity.py:33-48` 使用固定账号和 `demo123`；`app/api/dependencies.py:27-33` 永远返回演示身份源。
- **原始建议**：接入真实用户目录 / SSO / LDAP；生产环境禁用演示登录；JWT secret 通过服务器密钥注入并设置轮换策略。同时删除登录日志中的用户名、用户 ID、部门信息（`app/api/routes/auth.py:25-30`）。
- **⚠ 已决策（2026-08-29）**：用户决定**暂不引入真实用户目录 / SSO / LDAP**。理由：项目不面向真实用户，仅用于对面试官展示，演示认证可接受。后续如有真实用户需求再行替换。

### 2. 密钥管理

- **证据**：`.env` 中存在数据库、Redis、MinIO、模型 API 和 MinerU 凭据。
- **原始建议**：全部吊销并重新生成；生产只使用服务器 Secret 管理，不复制 `.env` 到镜像或前端。
- **⚠ 已决策（2026-08-29）**：用户决定继续使用现有 `.env`，**不替换为第二套独立凭据**。理由是更换流程繁琐且评估泄露风险可接受。此决策已确认，后续部署直接沿用当前 `.env` 中的密钥。

### 3. 补齐生产编排

- **证据**：仓库没有 `Dockerfile`、`compose.yaml`、`alembic.ini`。
- **改造**：增加 backend、frontend、worker、PostgreSQL+PGVector、Redis、MinIO、Nginx 的 Compose 配置。生产启动禁止 `--reload`，统一端口（当前 README、Vite 代理和部署文档存在 8000/8080、PostgreSQL/MySQL 不一致）。
- **✅ 状态（2026-08-30）**：已完成。新增根目录 `Dockerfile`、`compose.yaml`、前端多阶段镜像和 `frontend/nginx.conf`；前端通过 80 端口提供静态资源，`/api/` 反代 backend，SSE 已关闭代理缓冲并设置读超时。生产容器不启用 `--reload`。

### 4. 索引任务移出进程内

- **证据**：`app/services/indexing.py:195-204` 使用 `asyncio.create_task`。
- **风险**：进程重启、容器更新、多 worker 时任务会丢失或重复执行。
- **改造**：增加独立 worker，轮询 `kb_index_task`，使用数据库锁 / 租约领取任务；启动时恢复超时的 `PROCESSING` 任务。
- **⚠ 已决策（2026-08-29）**：用户明确**无需“上传文档后即使重启也能继续索引”**。演示过程中不重启服务，因此本项**不做改造**，保留当前进程内异步执行方案。

### 5. 数据库迁移和备份恢复

- **证据**：当前只有手工 SQL 和零散迁移文件，没有 Alembic 执行入口。
- **改造**：使用版本化迁移；发布前执行迁移；每日 `pg_dump`；MinIO 开启版本保护或同步备份；至少演练一次恢复。生产数据库不要使用 root，限制网络只允许应用内网访问。
- **⚠ 状态（2026-09-11）**：旧的手工 schema、全量 dump、零散迁移和 PostgreSQL 引导脚本已删除。数据库结构基线将在后续重新生成；当前版本完成基线初始化前不用于全新部署。

### 6. 保护监控端点并补健康检查

- **证据**：`app/main.py` 直接暴露 `/metrics`；部署文档明确没有应用层 ACL。
- **改造**：Prometheus / Grafana 只走内网或 Nginx 白名单；健康检查拆为存活和就绪，至少探测 PostgreSQL、Redis、MinIO。当前 `/api/v1/health` 仅返回固定 `UP`，不能判断服务是否可用。
- **✅ 状态（2026-08-30）**：已完成就绪探针 `/api/v1/health/ready`，检查 PostgreSQL、Redis 和 MinIO 服务可达性；Prometheus/Grafana 端口在 Compose 中仅绑定 `127.0.0.1`，通过 SSH 隧道访问。公网仍需在云防火墙中限制 80 端口之外的管理入口。

## P1：首个生产版本建议完成

### 1. 限制上传请求和内存

- **证据**：`app/integrations/minio.py:21-38` 一次性读取整个文件。
- **改造**：Nginx 设置 `client_max_body_size`；应用按字节流读取并强制上限；校验真实文件类型，不只信任扩展名；限制并发索引任务和临时磁盘。

### 2. 增加公网访问防护

- **改造**：登录、上传、聊天、评估接口增加 Nginx 限流；评估接口限制管理员和调用频率。设置请求超时、模型调用超时、最大并发数。SSE 代理关闭 buffering，并配置合理的读超时。
- **✅ 状态（2026-08-30）**：已完成。`frontend/nginx.conf` 对登录、上传、同步/流式聊天和评估入口按客户端 IP 分别限流，评估接口仍由应用层 `require_admin` 强制管理员权限；反向代理补充连接、发送、读取、请求体和 SSE 超时，并关闭 SSE buffering。后端 ChatOpenAI、Embedding 和 Reranker 使用显式 provider 超时；Uvicorn 通过 `SERVER_LIMIT_CONCURRENCY`、`SERVER_TIMEOUT_KEEP_ALIVE_SECONDS` 和 `SERVER_TIMEOUT_GRACEFUL_SHUTDOWN_SECONDS` 控制并发与连接生命周期。

### 3. 修复前端 XSS 风险

- **证据**：`jc-rag-kb-front/src/pages/Chat.tsx:432-435` 使用 `rehype-raw` 渲染模型内容。
- **改造**：优先移除 `rehype-raw`；必须保留时增加 HTML 白名单清洗并配置 CSP。长期可将 Token 从 `localStorage` 迁移到 HttpOnly Cookie。

### 4. 补数据完整性约束

- **证据**：`kb_doc_chunk`、文档、知识库之间缺少关键外键和一致性约束。
- **改造**：补 `doc_id` / `kb_id` 关联约束，增加删除和版本状态检查，防止异常数据造成跨知识库检索。

### 5. 规范日志和日志保留

- **改造**：禁止记录 JWT、问题全文、回答全文、文档正文、对象路径及普通权限场景下的用户 / 知识库标识。增加容器日志轮转、集中收集和告警。

### 6. 固定依赖和构建结果

- **改造**：后端使用 `uv sync --locked`；前端补 `package-lock.json` 并使用 `npm ci`。对基础镜像和依赖做漏洞扫描，避免使用浮动版本。
- **✅ 状态（2026-08-30）**：已完成。后端 Dockerfile 使用 `uv sync --locked --no-dev`，前端使用已有 `package-lock.json` 和 `npm ci`；Python、Node、Nginx、PGVector、Redis 等基础镜像改为明确版本标签，backend/frontend 使用可注入的发布镜像名。发布前执行 `bash deploy/security-scan.sh`，扫描源码、锁文件、Compose 配置及实际构建镜像；生产环境应将 `BACKEND_IMAGE`、`FRONTEND_IMAGE` 设置为带版本号或 Git SHA 的标签，并通过 `uv lock --check`、`npm ci` 验证锁文件未漂移。

## P2：暂时可不做

- Kubernetes、服务网格、自动扩缩容、多地域高可用。
- 独立 `tenant_id`，当前按 `kb_id` 隔离的设计可以保留。
- 复杂消息队列；单机先使用数据库任务表 + 独立 worker 即可。
- OpenTelemetry 全链路导出，可先保留 Prometheus 和结构化日志。

## 当前验证状态

- `uv run pytest -q`：387 通过，1 个弃用警告。
- `uv run ruff check .`：通过。
- `frontend` `npm run build`：通过；Vite 仅提示 Markdown/Ant Design vendor chunk 超过 500 kB。
- `uv run mypy app`：当前有 13 个类型检查错误，建议在生产 CI 中修复或明确豁免。

## 最小可上线顺序

密钥和认证 → Compose / Nginx → 数据库迁移备份 → 独立 worker → 健康检查 / 监控隔离 → 上传与限流 → 公网验收
