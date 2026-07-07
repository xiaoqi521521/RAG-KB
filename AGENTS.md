# AGENTS.md

本项目是对 `docs/references/` 中 Spring AI 版企业级 RAG 知识库项目的 LangChain/Python 重构。后续所有 Agent 在本仓库工作时，必须优先遵守本文件，再参考具体任务说明。

## 项目目标

构建企业内部知识库问答系统，支持文档上传、解析、分块、向量化、混合检索、Reranker 精排、引用溯源、防幻觉回答、多租户权限隔离、流式输出、评估与监控。

这不是一个只跑通向量检索的 Demo。实现时要保留参考项目的企业级工程要求：

- 文档处理链路可重试、可观测、可追踪状态。
- 查询链路可降级，尤其是 Reranker 超时后要回退到 RRF 排序结果。
- 回答必须基于检索上下文，知识库没有的内容要明确拒答。
- 引用来源要可追溯到文档、页码、章节或 chunk。
- 权限隔离必须在检索层硬过滤，不能依赖 Prompt。
- 关键参数、Token 成本、检索耗时和评估指标要可配置、可记录。

## 参考资料

主要参考 `docs/references/`：

- `01-rag-kb项目介绍.md`：项目目标、业务场景、原始技术栈。
- `03-技术架构设计.md`：两条管道、混合检索、Reranker、权限隔离。
- `05-数据库设计.md`：业务表、chunk 表、权限关系、对话记录。
- `07-文档加载层——多格式解析.md` 到 `17-引用溯源与防幻觉校验.md`：RAG 核心链路。
- `18-流式输出与多轮对话.md` 到 `21-Token 成本控制与监控.md`：工程质量、权限、评估和成本。

技术栈对比与替换方案维护在 `docs/plans/01-技术栈.md`。

如果需要生成临时分析、草稿、迁移说明或额外设计材料，统一放入 `docs/agens-output/`。不要把临时文件散落到项目根目录。

## 目标技术栈

后端以 Python 3.12 + FastAPI + LangChain 为主：

- AI 编排：LangChain Python，复杂状态流可引入 LangGraph。
- LLM：DeepSeek `deepseek-v4-flash`，通过阿里百炼大模型平台 OpenAI-compatible API 接入。
- Embedding：`text-embedding-v3`，通过 OpenAI-compatible embedding 客户端接入。
- Reranker：`gte-rerank` / `gte-rerank-v2`，封装为独立服务，不强绑定 LangChain。
- 数据库：PostgreSQL + PGVector，同实例承载业务表和向量表。
- 缓存：Redis 7.x，用于 embedding 缓存、查询缓存、任务进度。
- 文件存储：MinIO，保存原始上传文档。
- 权限：FastAPI JWT + RBAC + 数据库权限关系。
- 前端：React，承载对话界面和文档管理。
- 部署：Docker Compose。
- 监控：Prometheus metrics + Grafana。
- 评估：RAGAS + 自定义 Hit Rate / MRR / Faithfulness 流水线。

## 架构原则

系统分为两条主链路。

离线索引管道：

```plain
上传文档
  -> 保存原始文件到 MinIO
  -> 创建文档记录和索引任务
  -> 解析 PDF / Word / Markdown / Text
  -> 分块并保留来源元数据
  -> 批量 Embedding，优先使用缓存
  -> 写入 PostgreSQL + PGVector
  -> 更新任务状态，失败可重试
```

在线查询管道：

```plain
用户问题
  -> 认证与权限解析
  -> 查询改写，支持 HyDE 和多路查询
  -> 向量检索 + PostgreSQL 全文检索
  -> RRF 融合排序
  -> Reranker 精排，超时降级
  -> Token 预算裁剪
  -> 引用来源组装
  -> 基于上下文生成回答
  -> HTTP / SSE 流式返回
```

## LangChain 使用边界

LangChain 用来承接模型、embedding、document loader、text splitter、retriever、tool/agent 等通用能力。项目核心业务逻辑要保持在清晰的服务层，不要把权限、降级、引用溯源、评估逻辑塞进 Prompt 或单个 Chain。

推荐边界：

- `DocumentLoader` / `RecursiveCharacterTextSplitter` 可用于解析和分块，但必须补充业务元数据。
- `langchain-postgres` 的 PGVector 可用于向量存取，但混合检索和权限过滤可以直接写 SQL。
- `create_agent` 适合把检索封装成工具，但基础问答链路优先保持显式服务编排。
- Reranker、RRF、Token 裁剪、引用构建应作为独立模块，方便测试和降级。

不要使用只支持向量检索的一行式 RAG 抽象替代完整查询管道。

## 推荐目录结构

后续实现时优先采用以下结构，可随实际代码演进微调：

```plain
app/
  api/
    routes/
  core/
    config.py
    logging.py
    security.py
  db/
    session.py
    models.py
    migrations/
  schemas/
  services/
    document_loader.py
    chunking.py
    embedding.py
    indexing.py
    query_rewrite.py
    hybrid_retriever.py
    rrf.py
    reranker.py
    context_trimmer.py
    source_builder.py
    rag_query.py
    permissions.py
  repositories/
  integrations/
    bailian.py
    dashscope.py
    minio.py
    redis.py
  evaluation/
tests/
docs/
```

## 数据与权限要求

- 每个文档、chunk、会话、索引任务都必须带 `tenant_id` 或等价隔离字段。
- 检索 SQL 必须包含允许访问的 `kb_id` / `tenant_id` 过滤条件。
- 管理员跨知识库查询也要通过显式权限列表实现。
- 严禁把“只能访问某知识库”作为纯 Prompt 规则。
- 引用元数据至少包含：`document_id`、`document_name`、`kb_id`、`chunk_id`、`page_number` 或 `section_title`。

## RAG 质量要求

- 分块默认使用滑动窗口，chunk size 和 overlap 从配置读取。
- Embedding 建库和查询必须使用同一模型和同一维度。
- 混合检索默认召回向量 TopK 和全文 TopK，再用 RRF 融合。
- Reranker 必须设置超时时间和降级路径。
- 上下文必须按 Token 预算裁剪，不能无限拼接。
- 低置信度或无检索结果时，回答必须说明“未找到相关内容”。
- Prompt 中要明确：只根据参考内容回答，禁止使用通用知识补全公司政策。

## 开发约定

- 使用 `uv` 管理 Python 依赖和命令。
- 新功能优先写测试，尤其是权限过滤、RRF、Reranker 降级、引用溯源、防幻觉拒答。
- 业务逻辑保持小模块，避免把完整 RAG 管道写成一个巨型函数。
- 配置放入环境变量和配置对象，不在代码中硬编码 API Key、连接串、模型名。
- 外部服务调用要有超时、重试、错误日志和可观测指标。
- 修改参考资料衍生设计时，同步更新 `docs/plans/` 或 `docs/agens-output/` 中的对应说明。

## AI 执行规范

后续 AI 在本仓库执行任务时，必须遵守以下约束：

### 边界与架构

- **严格边界**：先明确本次任务的 `in scope` 和 `out of scope`。不得顺手增加未要求的功能、抽象、兼容层、配置项或重构。
- **先读后写**：编写新逻辑前，必须先了解项目现有的架构分层与设计模式。优先复用现有工具类和基类，禁止另起炉灶。
- **命名规范**：命名保持简洁、准确、贴合现有代码风格。避免为普通函数、变量、类起过长或概念过重的名字。

### 编码与实现

- **拒绝过度设计**：不做非必要兜底。只有当需求、参考文档、生产可靠性或已有设计明确要求时，才添加降级、兼容、重试、fallback。
- **原生优先**：优先使用成熟依赖、标准库、框架原生能力和项目已有工具。不要为了展示能力而手写已有库能稳定完成的功能。
- **拒绝幻觉**：涉及 FastAPI、LangChain、SQLAlchemy、Redis、MinIO、RAGAS、PGVector 等具体库 API 时，必须优先查阅官方文档或当前版本源码，严禁凭记忆臆造调用方式。
- **轻量处理**：简单文档、配置、说明类任务保持轻量处理；除非影响运行行为，不额外添加测试代码或无关文件。

### 代码组织与 Review 协作

- **不要切得太碎**：不要为了形式上的模块化拆出大量只有一两行、且没有独立语义价值的方法。只有在职责清晰、明显复用或确实能降低主流程理解成本时，才抽方法。
- **顺序贴合执行流程**：方法、函数和类内代码块尽量按真实执行顺序排列。优先组织为：对外入口方法 -> 核心主流程方法 -> 主流程直接调用的辅助方法 -> 状态更新和收尾方法。
- **减少审阅跳转**：强相关方法不要分散到文件不同位置。优先保证代码顺着往下读就能理解，减少 review 时反复跳转。

### 注释服务于 review 和理解

- 所有注释使用中文，保持简洁、清晰、有信息量。
- 在关键类和关键方法顶部补充简要说明；必要时写清 Args 和 Returns。
- 在核心业务流程中添加少量步骤注释，帮助我快速看懂执行骨架。
- 注释重点解释“为什么这样做”，尤其是复杂判断、边界处理、异常处理、重试逻辑和状态流转。
- 不要写重复代码字面意思的废话注释。
- 示例参考：
   - 示例用于对齐风格，不要机械复制示例中的业务名、类名或实现细节。

   函数/方法级注释示例：

   ```python
   def trim_context(chunks: list[RetrievedChunk], max_tokens: int) -> list[RetrievedChunk]:
       """按 token 预算裁剪检索上下文。

       Args:
           chunks: 已按相关性排序的候选 chunk 列表。
           max_tokens: 允许进入 Prompt 的最大 token 数。

       Returns:
           未超过预算的 chunk 子集，保留原始排序。
       """
   ```

   关键流程与 Why 注释示例：

   ```python
   # 第一步：优先保留高相关 chunk，避免后续裁剪破坏引用顺序。
   selected_chunks = []

   # 先写入新版本再清理旧版本，避免索引失败导致线上查询无可用数据。
   replace_chunks_with_new_version(document_id, new_chunks)
   ```


### 测试与质量

- **环境与依赖**：引入新包时必须使用项目指定的包管理工具（如 `uv`），并同步更新依赖配置文件，禁止随意破坏依赖树。
- **纯净生产**：不得为了测试方便污染生产代码。禁止添加仅测试使用的分支、参数或绕过逻辑；临时引入的测试辅助设计，测试通过后必须删除。
- **行为验证**：测试优先验证真实行为，少 mock 内部实现。测试应推动更清晰的接口，而不是倒逼生产代码暴露细节。
- **闭环验证**：完成前必须做与变更范围匹配的验证。代码变更至少运行相关测试或静态检查；文档变更至少重新读取目标文件确认落盘。


## 验证建议

常用验证命令根据项目成熟度逐步补齐：

```powershell
uv run pytest -v
uv run ruff check .
uv run mypy app
```

如果项目尚未配置对应工具，不要假装验证已通过。应说明当前缺少哪类验证，并优先补齐最小可运行测试。
