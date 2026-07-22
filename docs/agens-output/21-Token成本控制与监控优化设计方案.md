# Token 成本控制与监控优化设计方案

状态：设计已收敛，尚未实施。

本文基于当前代码、`CONTEXT.md`、ADR-0006、项目 `.env` 和用户确认的设计决策，目标是把现有三类 Token 统计扩展为可导出、可查看、可告警的六类 Token 监控，同时保留当前用户累计用量接口。

## 1. 设计结论

系统采用两个用途不同的数据面：

- Prometheus/Grafana：服务运行监控，按 `model`、`token_type`、`kb_id` 查看全局聚合趋势和告警状态。
- Redis：按用户持久化六类在线请求 Token 累计，供 `/api/v1/stats/tokens` 返回个人用量和成本估算。

六类 Token 采用互斥统计桶：

| `token_type` | 统计口径 |
| --- | --- |
| `embedding` | Embedding provider 输入 Token |
| `input` | 聊天模型输入 Token，包含 System Prompt、用户问题、对话历史和检索上下文 |
| `answer_generation` | 最终回答模型输出 Token |
| `hyde` | HyDE 模型输出 Token |
| `faithfulness_check` | 忠实性检测模型输出 Token |
| `reranker` | Reranker provider 返回的 Token 用量 |

`input` 不包含 Embedding 或 Reranker 输入；三个生成阶段只统计输出。因此六类桶可以相加，不重复计算同一批 Token。

## 2. 范围

### 2.1 In scope

- 通过现有 `prometheus_client` 默认 registry 导出 Token Counter，继续复用 `/metrics`。
- 为六类 Token 增加 `model`、`token_type`、`kb_id` 标签。
- 单知识库请求使用真实 `kb_id`；多知识库请求使用 `kb_id="multi"`，不复制或按比例估算。
- Embedding、聊天输入、最终回答、HyDE、Reranker、忠实性检测同步写入用户 Redis 累计。
- 使用新的 Redis 统计版本，不迁移既有三类统计。
- 扩展 `/api/v1/stats/tokens` 返回六类 Token、总量和成本估算。
- 增加全局每日 Token 预算闸门、80% 预警、100% 拦截和单次 20,000 Token 异常告警。
- 增加 Prometheus 规则和 Grafana Dashboard；告警本期只展示状态，不接外部通知。

### 2.2 Out of scope

- 不保存单次请求级 Token 明细或成本账本。
- Redis 不按模型分桶；用户累计成本按当前部署单价估算。
- 不把 `user_id`、部门 ID、请求 ID、问题、回答、文档 ID作为 Prometheus 标签。
- 不引入独立 `tenant_id`，不新增按用户或知识库分别计算的预算。
- 不把 Prometheus 作为预算拦截依据。
- 不增加 `/metrics` 的监控内网 ACL。
- 不接入邮件、钉钉、企业微信、Webhook 或 Alertmanager 通知渠道。
- 不迁移既有 `embeddingTokens`、`contextTokens`、`generationTokens` 数据。

## 3. 当前实现与缺口

当前实现已经具备：

- `TokenMetrics` 使用 OpenTelemetry Counter 记录 Embedding、Context、Generation。
- Redis 按用户累计三类字段，普通 Token 写入失败不阻断问答。
- Embedding 客户端读取 provider `usage.total_tokens`。
- LangChain 回答消息读取 `usage_metadata.output_tokens` 或 `response_metadata.token_usage.completion_tokens`。
- Reranker 客户端解析 `usage.total_tokens`，但尚未写入 TokenMetrics。
- FastAPI 已使用 `prometheus-fastapi-instrumentator` 暴露 `/metrics`，当前主要是 HTTP 指标。

主要缺口：

- OpenTelemetry Counter 没有 Prometheus MetricReader/Exporter，Token Counter 不会导出。
- HyDE 当前只返回文本，丢失模型响应中的 usage。
- 聊天输入 Token 尚未从 provider usage 提取。
- Reranker、HyDE、忠实性检测未进入用户 Redis 累计。
- Redis 统计字段和用户统计 API 仍是三类旧口径。
- 没有全局每日预算状态、拦截逻辑和 Prometheus/Grafana 告警规则。

## 4. 指标导出设计

### 4.1 应用内出口

Token 指标直接使用 `prometheus_client.Counter`，注册到现有默认 registry。`Instrumentator` 继续负责 HTTP 指标和 `/metrics` 暴露，不新增 OpenTelemetry Prometheus exporter，避免维护两个 Token 指标出口。

建议指标：

```text
rag_token_usage_total{model,token_type,kb_id}
rag_token_usage_unavailable_total{model,token_type,kb_id}
rag_token_budget_used_tokens{scope="global"}
rag_token_request_tokens{kb_id}
rag_token_request_over_limit_total{kb_id}
rag_token_budget_rejected_total{reason}
rag_token_write_failure_total{sink,token_type}
```

其中：

- `rag_token_usage_total` 是六类 Token 的核心 Counter。
- `rag_token_usage_unavailable_total` 记录 provider 没有可靠 usage 的调用次数，不把近似值写入 Token Counter。
- `rag_token_budget_used_tokens` 暴露 Redis 中当天全局预算已用量，供面板展示和告警查询；多实例部署时按同一全局值去重，不做实例求和。
- `rag_token_request_tokens` 使用 Histogram 观察一次请求的总 Token 分布，不带请求 ID或 `model` 标签，因为一次请求可能调用多个模型。
- `rag_token_request_over_limit_total` 在一次请求实际总量超过 20,000 时递增，作为告警触发依据，不带 `model` 标签。
- `rag_token_budget_rejected_total` 记录全局预算拦截次数。
- `rag_token_write_failure_total` 记录 Redis 或 Prometheus 写入失败，标签只使用固定的 `sink` 和 `token_type`。

所有 Token 指标都不包含用户、部门、请求、问题、回答、文档或对象路径标签。`model` 只使用当前配置的 Embedding、聊天和 Reranker 模型名。`kb_id` 当前 4 个值，多个知识库统一为 `multi`。

### 4.2 Provider usage 规则

- Embedding：记录 provider `total_tokens`，对应 `embedding`。
- 聊天模型输入：优先读取 LangChain `usage_metadata.input_tokens`，兼容 OpenAI 风格 `prompt_tokens`，对应 `input`。
- 最终回答、HyDE、忠实性检测：读取 provider `output_tokens`/`completion_tokens`，分别对应三个输出阶段。
- Reranker：记录 provider `usage.total_tokens`，对应 `reranker`。
- usage 缺失、非法或负数时只记录 `usage_unavailable` 和无敏感内容日志，不使用 `tiktoken` 估算冒充 provider 精确用量。
- 缓存命中、没有实际 provider 调用的阶段不递增 Token。
- Reranker 跳过、超时或降级且没有 provider usage 时不递增 `reranker`。

所有记录点都通过统一的 Token Usage Recorder 写入 Prometheus 和 Redis。两个出口不是一个事务；任一出口失败不影响问答主流程。

## 5. Redis 用户累计

### 5.1 新版本 Hash

新统计使用版本化命名空间：

```text
key: rag:token-stats:v2:<user_id>
fields:
  embeddingTokens
  inputTokens
  answerGenerationTokens
  hydeTokens
  rerankerTokens
  faithfulnessTokens
```

旧版本字段不迁移、不参与新统计。Hash 不按模型分桶，不保存单次请求明细，也不设置业务 TTL。没有当前用户的离线索引 Embedding 不写用户 Hash，但仍可写 Prometheus 的知识库维度指标。

### 5.2 个人统计 API

继续使用：

```text
GET /api/v1/stats/tokens
```

响应数据扩展为：

```json
{
  "embedding_tokens": 0,
  "input_tokens": 0,
  "answer_generation_tokens": 0,
  "hyde_tokens": 0,
  "reranker_tokens": 0,
  "faithfulness_tokens": 0,
  "total_tokens": 0,
  "estimated_cost": "0.0000",
  "currency": "CNY"
}
```

接口仍只读取当前认证用户，不接受用户 ID、知识库 ID或时间范围。Redis 读取失败返回 `503`，合法但没有新版本统计时返回全零。

## 6. 成本估算

当前 `.env` 已配置以下单价：

| 成本类型 | 当前配置 | 计费单位 |
| --- | ---: | --- |
| Embedding | `0.0005` | 元/1K Token |
| 聊天输入 | `0.001` | 元/1K Token |
| 聊天输出 | `0.002` | 元/1K Token |
| Reranker | `0.0005` | 元/1K Token |

Reranker 单价来自用户提供的百炼官方 `qwen3-rerank` 页面，页面标示为 `0.5 元/百万 Token`，换算为 `0.0005 元/1K Token`：

<https://bailian.console.aliyun.com/cn-beijing?tab=model#/model-market/detail/qwen3-rerank?serviceSite=asia-pacific-china>

成本公式：

```text
embedding_cost = embeddingTokens / 1000 * embedding_price
input_cost = inputTokens / 1000 * chat_input_price
answer_cost = answerGenerationTokens / 1000 * chat_output_price
hyde_cost = hydeTokens / 1000 * chat_output_price
faithfulness_cost = faithfulnessTokens / 1000 * chat_output_price
reranker_cost = rerankerTokens / 1000 * reranker_price
estimated_cost = sum(all six costs)
```

单价必须来自部署配置，不在业务代码中硬编码。Redis 只按 Token 类型累计，不按模型保留价格快照，因此模型或单价改变后，历史累计成本仍是当前配置下的估算，不是供应商账单。

## 7. 全局预算闸门

### 7.1 预算数据

全局每日预算默认：

```text
1,000,000 Token / day
```

预算时区默认 `Asia/Shanghai`，通过配置覆盖。Redis 保存当天全局计数，例如：

```text
rag:token-budget:v1:2026-07-22
```

当天计数可以设置为日期结束后保留一段时间，用于故障排查；它不是用户账单。

### 7.2 请求流程

```text
认证与知识库权限校验
  -> 查询缓存判断
  -> Redis 原子读取当天预算
     -> 已达到预算：拒绝新请求
     -> 未达到预算：进入模型调用
  -> provider 返回 usage
  -> 写六类 Token、用户 Redis、全局日计数和 Prometheus
  -> 请求结束，计算本次总量
```

预算达到 80% 时触发 Warning；达到 100% 时拒绝后续新请求。已经开始的请求继续完成，若完成后超过预算，记录超预算状态和告警。并发请求通过 Redis 原子判断避免在已经达到预算后继续启动；允许多个在预算尚未达到时已开始的请求共同造成少量超额。

预算闸门 Redis 读取或原子判断失败时返回 `503`，不调用模型；普通 Token 统计写入失败仍只记录告警，不阻断已经执行的问答。预算拒绝建议使用 `429 Too Many Requests`，响应消息明确说明今日 Token 预算已用尽。

## 8. Prometheus 和 Grafana

### 8.1 采集链路

```text
FastAPI /metrics
      -> Prometheus scrape
      -> recording rules / alert rules
      -> Grafana dashboard
```

Grafana/Prometheus 仅对系统管理员和运维开放。当前阶段不增加 `/metrics` 的网络 ACL，保持现有访问方式；后续可以通过反向代理或私有网络补充限制。

### 8.2 Dashboard 面板

首版建议提供四组面板：

1. 总览：今日 Token 总量、5 分钟 Token 速率、估算成本、预算使用比例、预算拒绝次数。
2. 维度拆分：按 `token_type`、`model`、`kb_id` 筛选和对比 Token 速率、小时增量、日增量。
3. 阶段成本：Embedding、最终回答、HyDE、Reranker、忠实性检测的 Token 和估算成本。
4. 异常治理：usage 缺失、单次超过 20,000 Token、预算 80%/100%、Redis 写入失败。

成本图表使用当前配置单价计算并明确标注“估算”，不能作为供应商账单。

### 8.3 告警规则

- `token_budget_warning`：全局当天使用量 `>= 800,000`，Warning。
- `token_budget_exhausted`：全局当天使用量 `>= 1,000,000`，Critical，并由应用预算闸门拦截后续请求。
- `single_request_token_limit`：最近窗口出现一次请求总量 `> 20,000`，Critical，提示异常查询或实现 Bug。
- `token_usage_unavailable`：usage 不可用次数持续增加，Warning，提示成本可能低估。
- `token_sink_write_failure`：Redis 或 Prometheus 记录失败，Warning。

本期告警只在 Grafana 展示状态，不配置外部通知渠道。

## 9. Redis 数据风险与运维要求

Redis 的原子递增解决的是并发修改，不等于持久化和 exactly-once：

- RDB 快照或 AOF 配置不当可能丢失最近写入。
- 主从异步复制和故障切换可能丢失最新计数。
- 网络超时重试可能造成重复递增。
- 后台忠实性检测进程异常退出时可能来不及写入。

因此部署建议开启 AOF/RDB、复制和备份，统计专用 Redis 使用 `noeviction`，并监控 Redis 可用性、AOF 错误、复制延迟和磁盘空间。当前设计接受统计低估或重复的灾难性风险，不宣称账单级准确；未来若需要账单级准确性，必须增加持久化聚合或幂等事件记录。

## 10. 实施顺序

1. 新增 Token 类型、usage 提取器、统一 Recorder 和 v2 Redis Hash。
2. 接入 Embedding、聊天输入/输出、HyDE、Reranker、忠实性检测的真实调用点。
3. 将 Token Counter 从 OpenTelemetry 切换为 `prometheus_client`，保留 `/metrics`。
4. 扩展用户统计 Schema、成本服务、Reranker 单价配置和 `/stats/tokens`。
5. 增加全局每日预算 Redis 计数、请求前闸门和 `429/503` 错误语义。
6. 增加 Prometheus recording/alert rules、Grafana Dashboard provisioning 和 Compose 监控配置。
7. 完成 Redis 故障、usage 缺失、并发预算、单次超限、权限与标签隐私测试。

## 11. 测试重点

- 六类 Token 的 provider usage 提取和错误值处理。
- HyDE、同步回答、SSE 回答、Reranker、忠实性检测各只记录一次。
- Redis v2 字段、旧数据不迁移、用户 API 六类响应和成本公式。
- 单知识库与多知识库 `multi` 标签归属。
- 无当前用户的离线 Embedding 只写 Prometheus，不写用户 Redis。
- Prometheus 指标不出现用户、部门、请求、问题和文档标签。
- Redis 统计写失败不阻断问答；预算闸门 Redis 故障返回 `503`。
- 预算 80% 告警、100% 拦截后续请求、已开始请求可完成。
- 并发请求下预算判断、单次超过 20,000 Token 告警和 usage 缺失告警。
- `/metrics` 可暴露 Token 指标，Grafana 规则可查询六类 Token 和成本估算。
