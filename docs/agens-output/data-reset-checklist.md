# 数据重置清单

> 目的：测试期产生的数据存在多处不一致（引用指向旧 chunk、评估版本对不上文档版本、Redis 中残留旧口径 Token 统计等）。项目已基本完善，按本清单重置后，后续演示与测试的数据将保持一致口径。
> 基准：`7f39d2b`（评估 Token 桶拆分与每日用量 Gauge）。

## 最终确认范围（2026-09-11）

经确认，本次只清理以下四类，其余保持不动：

- PostgreSQL：`kb_chat_session`、`kb_chat_message`、`kb_answer_feedback`、`kb_eval_result`、`kb_eval_run_usage`
- Redis：`rag:token:v3:` 下的 stats / daily / budget 三组键（保留 `rag:emb:*`、`rag:query:*`、`rag:rewrite:*` 缓存）
- 本地监控数据：`.scratch/runtime/grafana-data`、`.scratch/runtime/prometheus-data`、`D:\Code\tools\prometheus-local-data`
- `app/db/`：旧全量 dump 已删除，数据库清理完成后重新生成干净结构基线

明确不清理：文档与索引（`kb_document`、`kb_doc_chunk`、`kb_index_task`）、MinIO 对象、知识库与权限、`kb_eval_dataset` 及其 `expected_chunk_ids`。下文其余章节作为未来全量重置的参考保留。

注意事项：

- 保留 `kb_eval_dataset` 时，先检查 `source_feedback_id`：反馈表清空后该引用会悬空，需置空。
- Redis 清理必须按前缀删除，不能用 `FLUSHDB`（会误清要保留的向量与查询缓存）。
- 清空 `kb_eval_result` 后，下一轮评估版本号从 1 重新生成。
- 删除监控数据目录前先停止 Prometheus/Grafana 进程，避免 Windows 文件锁。

## 0. 重置原则

- 先停后端再清数据，避免索引任务、评估任务在重置过程中继续写入。
- PostgreSQL 各业务表之间**没有声明外键**，必须显式逐表清理，`CASCADE` 不会级联。
- 用 `TRUNCATE ... RESTART IDENTITY` 让序列回到 1，保证 `app/db/data.sql` 种子数据引用的 `kb_id` 1-4 依然成立。
- MinIO 对象、PostgreSQL 文档记录、Redis 缓存三者必须一起清，否则会出现孤儿对象或悬空引用。
- 保留项与重置项见各节标注；拿不准时优先重置，宁可重跑测试数据。

## 1. PostgreSQL（ragkb 库）

必须重置（数据间已互相矛盾）：

- [ ] `kb_doc_chunk`：chunk 与向量。重置后所有 `chunk_id` 重新生成，聊天引用与评估期望值必须重建。
- [ ] `kb_document`：`minio_path` 与 `version` 与 MinIO 对象、评估版本强绑定。
- [ ] `kb_index_task`：历史索引任务记录。
- [ ] `kb_chat_session` / `kb_chat_message`：`sources` JSON 内嵌 `document_id`/`chunk_id`，清库后全部失效；旧消息还缺少 `answer_mode`、`knowledge_base_searched` 等新列的有效值。
- [ ] `kb_answer_feedback`：反馈挂在旧消息上，且评估数据集可能通过 `source_feedback_id` 引用。
- [ ] `kb_eval_result`：`eval_version` 对应旧文档版本号，文档重置后必然错位。
- [ ] `kb_eval_run_usage`：按 `(kb_id, eval_version)` 唯一，旧记录会阻止新版本写入，且成本分桶是旧口径。

按需处理（重置后重新播种）：

- [ ] `kb_knowledge_base` / `kb_permission`：可整体 TRUNCATE 后重放 `app/db/data.sql`（含 4 个知识库、部门权限、hr002 的 ADMIN 授权）；若想保留现有知识库，则跳过，但需确认权限与演示预期一致。
- [ ] `kb_eval_dataset`：问题文本可以保留，但 `expected_chunk_ids` 必须在文档重新索引后回填；若 `source_feedback_id` 指向已删除的反馈需一并清掉或置空。

重置 SQL（psql 连接 ragkb 后执行）：

```sql
TRUNCATE kb_doc_chunk, kb_document, kb_index_task,
         kb_chat_message, kb_chat_session, kb_answer_feedback,
         kb_eval_result, kb_eval_run_usage, kb_eval_dataset
RESTART IDENTITY;

-- 若决定连知识库一起清：
TRUNCATE kb_permission, kb_knowledge_base RESTART IDENTITY;
```

## 2. Redis

所有键都在 `rag:*` 命名空间下，Token 统计类键**无 TTL、永久累计**，必须清：

- [ ] `rag:token:v3:stats:{user_id}`：用户累计 Token Hash（含已废弃的旧字段口径）。
- [ ] `rag:token:v3:daily:{yyyy}:{mm}:{yyyy-mm-dd}`：每日用量 Hash（永久保留，含旧口径数据，会直接污染 `/usage` 与 Grafana 今日面板）。
- [ ] `rag:token:v3:budget:cny:{yyyy}:{mm}:{yyyy-mm-dd}`：每日金额预算 Hash（不清则当日预算仍被测试消耗占用）。
- [ ] `rag:emb:*`：embedding 缓存（默认 7 天 TTL），清掉避免复用旧模型/旧维度向量。
- [ ] `rag:query:user-question:*`、`rag:query:user-question-hyde:*`、`rag:rewrite:*`：查询/HyDE/改写缓存（短 TTL 会自然过期，顺手清掉）。

```bash
# 该 Redis 实例专用时最简单：
redis-cli -h localhost FLUSHDB
# 与其他服务共用时按前缀删：
redis-cli --scan --pattern 'rag:*' | xargs -r redis-cli DEL
```

## 3. MinIO（bucket：`rag-documents`）

- [ ] 清空 `kb/` 前缀下全部对象：现行路径为 `kb/{kbId}-{知识库名}/{uuid8}-{文件名}`，历史数据还可能存在旧版 `kb/{kbId}/...` 前缀。

```bash
mc rm --recursive --force local/rag-documents/kb/
```

清 MinIO 与清 `kb_document` 必须同批完成，避免孤儿对象或下载 404。

## 4. 监控运行时数据

本地（无 Docker）：

- [ ] `.scratch/runtime/grafana-data`：本地 Grafana 数据目录（sqlite、面板状态）。
- [ ] `.scratch/runtime/prometheus-data` 与 `D:\Code\tools\prometheus-local-data`（`scripts/start-prometheus-local.ps1` 默认数据目录）：Prometheus TSDB 按 1 年保留，不删会继续画出旧趋势曲线。

应用进程内的 Prometheus Counter（`rag_token_usage_*` 等）随后端重启清零，无需单独处理。

服务器 Compose 部署时对应处理 named volume：`postgres-data`、`redis-data`、`minio-data`、`prometheus-data`、`grafana-data`。

## 5. 仓库内基线文件

- [ ] `app/db/data.sql`：保持现状，作为重置后的种子数据。

## 6. 前端本地状态（可选）

- [ ] 浏览器 localStorage 中的登录 token：清站点数据即可。

## 7. 重置后恢复步骤

1. 启动后端，确认 `GET /api/v1/health` 正常。
2. 若清了知识库，重放 `app/db/data.sql`（已包含 hr002 ADMIN 授权，无需再跑 `20260903_grant_hr002_hr_kb_admin.sql`）。
3. 用各演示账号重新上传文档，等待全部状态为已发布。
4. 回填评估期望：`SELECT id, LEFT(content, 50) FROM kb_doc_chunk WHERE kb_id = 1 ORDER BY id;` 后更新 `kb_eval_dataset.expected_chunk_ids`（KB 2、3 同理）。
5. 运行一轮评估，生成新版本报告与 `kb_eval_run_usage`。
6. 发起几条真实提问与反馈，随后检查 `/dashboard`、`/usage`、`/metrics` 和 Grafana 面板。
7. 一致性抽查：随机打开一条带引用的回答，确认 `sources` 中的 `chunk_id` 在新 chunk 中存在；评估报告的 `eval_version` 与对应文档 `version` 一致；全局用量的评估 Token 与 `kb_eval_run_usage` 汇总一致。
