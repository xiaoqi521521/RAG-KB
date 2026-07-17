# RAG-KB

企业级 RAG 知识库问答系统，使用 FastAPI、LangChain、PostgreSQL/PGVector、Redis、MinIO 和 RAGAS 构建。

项目目标不是只完成向量检索，而是提供可追踪、可降级、可评估的知识库问答链路：文档索引、混合检索、Reranker 精排、引用溯源、权限隔离、同步/流式问答、Token 成本统计和自动化评估。

## 主要能力

- 支持 PDF、Word、Markdown 和 Text 文档解析、分块、Embedding 与版本化索引。
- 向量检索与 PostgreSQL 全文检索通过 RRF 融合。
- Reranker 超时或失败时回退到 RRF 排序，避免查询链路整体失败。
- 回答引用可追溯到文档、页码、章节和 chunk；上下文不足时明确拒答。
- 知识库级权限隔离，检索 SQL 硬过滤授权知识库、当前文档版本和已完成索引。
- 支持 `POST /api/v1/chat` 同步问答和 `GET /api/v1/chat/stream` SSE 流式问答。
- 支持 Hit Rate@5、MRR@5、Faithfulness、Answer Relevancy、Context Recall 和 Context Precision 评估。
- 评估使用独立的 `RAGAS_MAX_TOKENS=4096` 和 `RAGAS_TIMEOUT_SECONDS=60`，不影响普通问答预算。

## 技术栈

| 层次 | 技术 |
| --- | --- |
| Runtime | Python 3.12、uv |
| API | FastAPI、Uvicorn、Pydantic Settings |
| AI 编排 | LangChain、RAGAS 0.4.3 |
| 模型 | 阿里云百炼 OpenAI-compatible API、DeepSeek/Qwen、text-embedding-v3 |
| 数据库 | PostgreSQL、PGVector、SQLAlchemy 2、Alembic |
| 基础设施 | Redis 7、MinIO |
| 观测 | OpenTelemetry、Prometheus instrumentation |
| 测试 | pytest、pytest-asyncio、Ruff、Mypy |

## 快速开始

### 环境要求

- Python 3.12+
- PostgreSQL（启用 `vector` 扩展）
- Redis 7+
- MinIO
- 阿里云百炼 API Key
- 可用的 Reranker 服务地址

### 安装

```bash
git clone git@github.com:xiaoqi521521/RAG-KB.git
cd RAG-KB
uv sync
cp .env.example .env
```

PowerShell 可使用：

```powershell
Copy-Item .env.example .env
```

编辑 `.env`，至少填写数据库、Redis、MinIO、模型 API Key 和 `RERANKER_ENDPOINT`。不要把 `.env` 提交到 Git。

### 初始化数据库

执行 `app/db/schema.sql`，再按部署版本依次执行 `app/db/migrations/` 中的迁移脚本。确保 PostgreSQL 已启用 PGVector 扩展。

### 启动服务

```bash
uv run uvicorn app.main:app --reload --host 0.0.0.0 --port 8000
```

健康检查：

```bash
curl http://localhost:8000/api/v1/health
```

## 常用接口

所有需要认证的接口使用 `Authorization: Bearer <token>`。演示登录接口：

```bash
curl --request POST http://localhost:8000/api/v1/auth/login \
  --header 'Content-Type: application/json' \
  --data '{"username":"admin","password":"demo123"}'
```

主要接口如下：

| 功能 | 方法 | 路径 |
| --- | --- | --- |
| 登录 | POST | `/api/v1/auth/login` |
| 知识库管理 | GET/POST | `/api/v1/kb` |
| 文档上传与管理 | GET/POST/DELETE | `/api/v1/kb/{kb_id}/documents` |
| RAG 查询 | POST | `/api/v1/rag/query` |
| 同步会话问答 | POST | `/api/v1/chat` |
| 流式会话问答 | GET | `/api/v1/chat/stream` |
| 提交回答反馈 | POST | `/api/v1/feedback/{message_id}` |
| 评估运行 | POST | `/api/v1/eval/{kb_id}/run?version={eval_version}` |
| 评估历史 | GET | `/api/v1/eval/{kb_id}/history` |
| Token 统计 | GET | `/api/v1/stats/tokens` |

反馈接口使用 JSON 请求体，而不是查询参数：

```bash
curl --request POST http://localhost:8000/api/v1/feedback/34 \
  --header 'Authorization: Bearer <token>' \
  --header 'Content-Type: application/json' \
  --data '{"feedback":1}'
```

反馈只能提交给当前用户自己的、未删除会话中的助手消息。

## 评估配置

正式评估只运行 `ACTIVE` 标准问题。检索指标只统计已标注 `expected_chunk_ids` 的问题；生成指标只统计填写 `expected_answer` 且没有拒答或 Reranker 降级的问题。

关键配置：

```dotenv
RAGAS_MAX_TOKENS=4096
RAGAS_TIMEOUT_SECONDS=60
RAG_CONTEXT_MAX_TOKENS=3000
RAG_RETURN_TOP_N=5
```

评估是同步请求，同一个知识库内 `eval_version` 只能运行一次。历史结果保留在 `kb_eval_result`，不可用的单项指标保持 `NULL`，不按 0 分计算。

## 测试与质量检查

```bash
uv run pytest -q
uv run ruff check .
uv run mypy app
```

## 项目结构

```text
app/
  api/             FastAPI 路由与依赖
  core/            配置、安全、数据库和观测
  evaluation/      RAGAS 评估适配器与评估编排
  integrations/    百炼、Embedding、MinIO 等外部服务适配器
  models/          SQLAlchemy 数据模型
  repositories/    数据访问层
  services/        RAG、索引、会话、反馈和成本统计服务
  schemas/         API 请求与响应模型
tests/             单元、API、服务和集成测试
docs/              设计、规格、ADR 和运行手册
```

运行日志保存在本地 `docs/logs/`，该目录已被 Git 忽略；其中可能包含本机路径、业务标识和调试堆栈，不应公开上传。
