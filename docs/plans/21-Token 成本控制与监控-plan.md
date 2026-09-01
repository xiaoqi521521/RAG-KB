# 21-Token 成本控制与监控 Plan

本文基于 [21-Token 成本控制与监控.md](../references/21-Token%20成本控制与监控.md)、当前 V4 RAG 查询管道以及本次设计访谈形成。本文只描述后续实施方案，不包含代码实现。

本阶段通过“首轮查询结果缓存 + 既有 Token 计量补全 + 当前用户成本统计 API”减少重复模型调用并提供近似成本观察。多轮追问的回答依赖对话历史，不使用查询结果缓存；权限校验始终发生在缓存读取之前。

## 1. 当前状态与缺口

项目已经具备以下基础：

- Redis 异步客户端以及 `query_cache_ttl_seconds=600` 配置。
- 文档 Embedding Redis 缓存；只有实际调用 provider 的未命中批次才记录 Embedding Token。
- `ContextTrimmer` 对裁剪后参考内容使用 `tiktoken` 计数并记录 `contextTokens`。
- 同步、SSE 和 V1-V4 查询链路能够从 provider usage 记录生成 Token。
- `TokenMetrics` 已把 `embeddingTokens`、`contextTokens`、`generationTokens` 按当前用户累计到 Redis Hash。
- 同步与 SSE 聊天会读取最近五轮历史并在成功后持久化消息、引用、Token 和耗时。
- 路由在检索前执行知识库范围的实时读权限校验。

现有缺口：

- 没有最终回答的查询结果缓存，重复的首轮 FAQ 仍执行完整检索和生成。
- `TokenMetrics` 没有读取当前用户累计值的接口，也没有成本计算服务和公开统计路由。
- 单价没有配置项，不能按实际部署模型更新成本估算。
- 当前问答耗时由不同服务各自计时，缓存命中后需要继续返回本次完整服务耗时。
- OpenTelemetry Token Counter 没有配置导出器；本阶段经确认不补 Prometheus/Grafana 成本监控。

## 2. 范围

### 2.1 In scope

- 为 `POST /api/v1/rag/query`、`POST /api/v1/chat` 和 `GET /api/v1/chat/stream` 的无历史首轮问答增加共享查询结果缓存。
- 缓存成功回答、引用来源和命中数量，默认 TTL 为 10 分钟。
- 所有入口在缓存读取前完成身份加载和请求指定的全部 `kb_ids` 读权限校验。
- 多轮追问继续把最近五轮历史提供给模型，但不读取或写入查询结果缓存。
- 缓存命中的聊天请求仍创建或复用会话并保存完整用户、助手消息和引用。
- 补充当前用户累计 Token 与人民币成本估算 API。
- 把 Embedding 输入、聊天输入和聊天输出单价改为环境配置。
- 对 Redis 缓存和统计故障提供不影响问答的降级路径。
- 增加与变更范围对应的缓存、权限、同步、SSE、统计和成本计算测试。

### 2.2 Out of scope

- 不缓存多轮追问，不把历史消息加入缓存键，也不做语义缓存。
- 不新增文档更新、删除或版本切换时的主动缓存失效。
- 不缓存拒答、异常响应、生成失败或没有引用来源的回答。
- 不修改文档 Embedding 缓存策略、上下文裁剪算法、Reranker 或 V4 检索排序。
- 不把离线索引 Embedding 成本归属给上传用户。
- 不统计 Reranker 成本。
- 不新增管理员成本报表、按日/月统计、数据库成本账本、归档或清理任务。
- 不新增 Token、成本或缓存的 Prometheus 指标、Grafana 看板或告警。
- 不追求供应商账单级精确计费，也不修改当前模型选择。

## 3. 已确认的设计决策

| 主题 | 决策 |
| --- | --- |
| 缓存资格 | 仅无历史消息的首轮问答可读写缓存；存在任意历史消息时完全跳过缓存 |
| 缓存身份 | 去除问题首尾空白后，与升序 `kb_ids` 组合并计算 MD5；不做大小写、标点或语义归一化 |
| 权限边界 | 每次请求先实时校验全部知识库读权限，再允许读取缓存；任一知识库无权则整体拒绝 |
| 缓存内容 | 只缓存有引用来源的成功回答；拒答和失败不缓存 |
| 一致性 | TTL 默认 10 分钟，不随文档内容变更主动失效，接受有效期内暂时不一致 |
| 会话行为 | 缓存命中仍保存本轮消息与引用；历史消息仍参与未命中追问的模型输入 |
| 耗时 | `latency_ms` 为本次完整服务实际耗时，缓存命中不返回固定 `0` |
| 用户统计 | 只统计在线问答产生的查询 Embedding、参考内容和生成 Token |
| 输入口径 | 聊天输入成本只以裁剪后的 `contextTokens` 近似，不含系统提示词、问题和历史 |
| usage 缺失 | provider 未返回生成 usage 时不本地估算补写，允许成本低估 |
| 金额 | 单价通过环境变量配置；币种固定人民币；API 返回 `currency=CNY` 和四位小数字符串金额 |
| 数据保留 | 用户 Redis Hash 无 TTL，生产 Redis 必须持久化 |
| Redis 故障 | 缓存读取失败按未命中继续；缓存写入和统计累计失败不影响回答 |
| 监控 | 本阶段不新增 Prometheus/Grafana 成本与缓存监控 |

这些决策均为当前模块内可调整的服务策略，没有形成同时满足“难以逆转、缺少上下文会令人意外、存在实质权衡”三个条件的架构决策，因此不新增 ADR。

## 4. 总体流程

### 4.1 无会话历史的同步问答

```plain
认证并实时加载当前用户
  -> 开始完整服务耗时计时
  -> 校验全部 kb_ids 读权限
  -> 确认无历史消息
  -> 查询结果缓存读取
     -> 命中：组装本次响应 -> 聊天入口保存消息 -> 返回
     -> 未命中或 Redis 失败：执行完整 V4 RAG
  -> 成功且 sources 非空
     -> 普通 RAG：写缓存
     -> 聊天入口：先保存消息，再写缓存
  -> 返回本次实际 latency_ms
```

`POST /api/v1/rag/query` 没有会话历史，始终具备缓存资格。`POST /api/v1/chat` 只有在新会话或复用会话尚无消息时具备缓存资格。

### 4.2 多轮追问

```plain
认证与全部 kb_ids 权限校验
  -> 复用会话并加载历史
  -> 发现历史非空，跳过缓存读写
  -> V4 检索、精排、过滤和上下文裁剪
  -> 系统消息 + 最近五轮历史 + 当前问题
  -> 模型生成、引用解析和消息持久化
```

缓存资格由服务端实际加载到的历史消息判断，不能只依据客户端是否传入 `session_id`。一个存在但尚无消息的会话仍属于无历史首轮；一个省略 `session_id` 的新会话也不能影响其他会话对相同首轮问题的缓存复用。

### 4.3 SSE 缓存命中

SSE 入口仍先完成权限校验并建立或复用会话。缓存命中后不进入检索与生成状态，按以下顺序返回：

```plain
status(RETRIEVING, session_id)
  -> token(完整缓存回答)
  -> 保存用户与助手消息
  -> done(sources, 本次实际 latency_ms)
```

完整答案作为一个 `token` 事件返回，保持前端协议兼容。只有消息持久化完成后才发送 `done`；保存失败按既有聊天失败语义处理，不能向客户端宣称完整成功。

## 5. 查询结果缓存设计

### 5.1 服务边界

新增独立 `QueryCacheService`，只负责：

- 构建缓存键。
- 从 Redis 读取并校验缓存值。
- 以 TTL 写入可缓存回答。
- 把 Redis 与反序列化失败降级为缓存未命中或写入失败。

它不负责权限判断、会话历史加载、消息持久化、检索、回答质量判断或 HTTP/SSE 输出。缓存资格由同步或流式问答编排器根据实际历史判断；响应是否有引用由编排器在写入前判断。

### 5.2 缓存键

逻辑输入：

```plain
normalized_question = question.strip()
sorted_kb_ids = sorted(kb_ids)
digest = md5(canonical_json([normalized_question, sorted_kb_ids]))
redis_key = rag:query:user-question:<digest>
```

使用结构化 JSON 序列化，避免字符串拼接在特殊字符或列表边界上产生歧义。`kb_ids` 在路由层去重并校验为正整数，缓存层仍排序以保证集合顺序不影响命中。

旧版首轮 key 与新 key 都使用 MD5，仅变更前缀；迁移脚本扫描旧 `rag:query:<md5>` 和 `rag:hyde:<md5>`，通过 Redis `RENAMENX` 原子迁移并保留原值 TTL。运行时新 key 未命中仍按当前问题计算旧 key 并兼容迁移。

缓存键不包含 `user_id`、`department_id` 或权限来源，因此相同问题可以在对同一知识库范围具有读权限的用户之间复用。每次读取前的实时权限校验是共享缓存成立的必要条件。缓存键摘要、问题、用户 ID 和知识库 ID 均不得写入日志或指标。

### 5.3 缓存值

缓存值采用版本明确的 JSON 结构，至少包含：

- `answer`
- `sources`
- `hit_count`

不缓存 `latency_ms`、`session_id`、用户信息、历史消息或 Token 用量。读取时使用 Pydantic Schema 校验；字段缺失、版本不兼容或来源结构非法时视为未命中，并尽力删除脏值。

缓存值只保存用户原本有权读取的回答和引用，但仍属于知识库派生内容。Redis 必须使用与应用数据级别相匹配的访问控制、网络隔离和持久化策略。

### 5.4 写入与过期

- TTL 复用现有 `query_cache_ttl_seconds`，默认 `600`。
- 仅在生成完成、引用解析成功且 `sources` 非空时写入。
- 明确拒答、无命中、无有效引用、超时、客户端中断和其他异常均不写入。
- 写入发生在响应完成前，但写失败不改变已经形成的正常回答。
- 不按文档变更主动删除缓存，也不引入知识库内容版本号。

### 5.5 缓存故障降级

- `GET`、反序列化或校验失败：记录 `operation=read`、结果和错误类型，按未命中执行完整 RAG。
- `SETEX` 失败：记录 `operation=write` 和错误类型，继续返回本次回答。
- 删除脏值失败：只记录错误类型，不重复抛出。
- 日志不记录问题、缓存键、回答、引用、用户 ID 或知识库 ID。

## 6. 与现有问答服务的集成

### 6.1 显式集成点

缓存不能放进 `RagQueryServiceV4`：该服务不知道聊天历史是否为空，而 V1-V3 普通 RAG 路由也需要使用缓存。只共享 `QueryCacheService`，不再增加跨三类入口的缓存包装层：

- 普通 RAG 路由在权限校验后读缓存，未命中时调用已配置的具体 RAG 管道，成功后写缓存。
- `SynchronousChatService` 创建或复用会话、加载实际历史后决定是否读缓存；未命中时继续现有 V4 流程。
- `StreamingChatService` 以相同历史规则决定是否读缓存，并负责缓存命中时的 SSE 事件和消息持久化。

三处入口可以有少量顺序编排代码，但键构建、序列化、Redis 降级和 TTL 必须复用 `QueryCacheService`。不得在缓存服务中复制 RRF、Reranker、上下文裁剪、引用或拒答逻辑，也不得绕过既有 V4 服务。

普通 RAG 成功后可以直接写缓存。同步与 SSE 聊天必须先成功保存本轮消息，再写入新缓存，避免会话事务失败的请求产生新的可复用答案；缓存写入失败仍不改变已经形成的正常回答。缓存命中不触发查询 Embedding、Reranker、生成模型或在线忠实性抽样，因此不新增本次 Token 消耗。

### 6.2 会话消息

同步与 SSE 缓存命中后仍按正常成功回答保存：

- 用户问题。
- 缓存回答。
- 缓存引用来源。
- `token_count=0`，表示本次没有发生生成模型调用。
- 本次完整服务 `latency_ms`。

保存后的消息可正常参与下一轮模型历史。下一轮因已有历史必定绕过缓存。

### 6.3 完整服务耗时

计时起点应前移到问答业务入口，覆盖当次实际执行的权限校验、历史读取、缓存访问、会话读写、检索和生成。响应中的 `latency_ms` 每次重新计算，不从缓存值读取。

该字段不是单独的检索耗时或模型耗时，也不包含客户端网络传输时间。同步响应在返回前停止计时；SSE 在发送 `done` 前计算最终值。

## 7. Token 统计口径

### 7.1 用户累计字段

继续使用现有 Redis Hash：

```plain
key: rag:token-stats:<user_id>
fields:
  embeddingTokens
  contextTokens
  generationTokens
```

Hash 不设置 TTL，语义是“自 Redis 中该用户统计数据开始保留以来的累计值”。生产环境必须开启 Redis 持久化；Redis 数据丢失后不能把重建后的统计宣称为历史完整账单。

### 7.2 归属规则

- `embeddingTokens`：只累计在线请求实际调用 provider 的查询、查询改写或 HyDE Embedding Token；缓存命中不累计。
- `contextTokens`：只累计本次实际进入模型参考内容的 Token；缓存命中不累计。
- `generationTokens`：只累计最终用户回答由 provider usage 返回的模型输出 Token；缓存命中不累计。
- 离线索引没有当前问答用户，不写用户 Redis Hash。
- Reranker 不进入用户统计。
- 查询改写、HyDE 生成和在线忠实性抽样等内部模型输出不进入本阶段用户成本统计；实现时必须保持调用来源隔离。

Embedding 和生成 provider 未返回 usage 时不伪造精确数值。日志只记录 usage 不可用的阶段与来源，不记录业务内容或标识。

### 7.3 Redis 写入失败

`TokenMetrics` 保持观测层不阻断主流程的原则。Redis `HINCRBY` 失败时记录字段类别和错误类型，回答继续；用户统计可能因此低于实际成本。统计 API 读取 Redis 失败属于该 API 自身不可用，应返回 `503`，不能伪装为全部为零。

## 8. 成本配置与计算

### 8.1 新增配置

建议在 `Settings` 中新增三个大于等于零的人民币单价配置，单位统一为“元 / 1000 Token”：

- `embedding_input_cost_cny_per_1k_tokens`
- `chat_input_cost_cny_per_1k_tokens`
- `chat_output_cost_cny_per_1k_tokens`

默认值不应复制参考文档中的 `qwen-plus` 价格。部署环境必须按实际 `embedding_model` 与 `chat_model` 设置；缺少生产单价时应用应在启动配置校验阶段失败，避免静默返回错误成本。

### 8.2 计算公式

使用 `Decimal` 计算：

```plain
embedding_cost = embeddingTokens / 1000 * embedding_input_price
context_cost = contextTokens / 1000 * chat_input_price
generation_cost = generationTokens / 1000 * chat_output_price
estimated_cost = embedding_cost + context_cost + generation_cost
```

`contextTokens` 只表示裁剪后的参考内容，不包含 System Prompt、当前问题和对话历史，所以 `estimated_cost` 是有意低估的近似值。最终金额按人民币四位小数四舍五入，并序列化为字符串；币种固定返回 `CNY`，不能通过配置改变，也不与供应商账单做自动对账。

### 8.3 当前百炼价格核验

本项目当前使用阿里云百炼中国内地（华北 2 / 北京）标准价，核验日期为 2026-07-17。价格直接取自百炼官方模型详情页，不沿用参考项目的示例单价：

- [`text-embedding-v3` 官方详情](https://bailian.console.aliyun.com/cn-beijing/?tab=model#/model-market/detail/text-embedding-v3)：文本输入 `0.5 CNY/百万 tokens`，折算为 `0.0005 CNY/1K tokens`。
- [`deepseek-v4-flash` 官方详情](https://bailian.console.aliyun.com/cn-beijing/?tab=model#/model-market/detail/deepseek-v4-flash)：输入 `1 CNY/百万 tokens`，输出 `2 CNY/百万 tokens`，分别折算为 `0.001` 和 `0.002 CNY/1K tokens`；页面同时列出缓存命中输入价 `0.2 CNY/百万 tokens`。

当前用户 Token 统计只有 `contextTokens`，没有缓存命中输入 Token 的独立字段，因此成本计算对上下文统一使用 `deepseek-v4-flash` 未命中输入价。以 `780 / 13356 / 2354` 三类 Token 为例，估算金额为 `0.0185 CNY`。

## 9. 成本统计 API

新增认证接口：

```plain
GET /api/v1/stats/tokens
```

该接口只读取当前认证用户，不接收用户 ID、知识库 ID 或时间范围。普通用户和管理员都只能通过该接口读取自己的累计值，不提供管理员跨用户聚合。

响应数据：

```json
{
  "embedding_tokens": 125000,
  "context_tokens": 890000,
  "generation_tokens": 210000,
  "total_tokens": 1225000,
  "estimated_cost": "0.6300",
  "currency": "CNY"
}
```

具体金额以部署配置为准，示例不构成默认单价。`total_tokens` 是三类统计的算术和，不代表一次模型请求的 provider `total_tokens`。

接口错误语义：

- 未认证：`401`。
- 当前用户身份数据源不可用：沿用认证依赖的 `503`。
- Redis 读取不可用或返回无法解析的统计值：`503`。
- 合法但尚无统计：三类 Token 和总量为 `0`，成本为 `"0.0000"`。

## 10. 安全与隐私

- 查询缓存只在完整权限校验通过后读取，不能把“缓存存在”作为授权依据。
- 指定多个知识库时任一无权即整体 `403`，不得读取部分范围缓存。
- 缓存不扩大检索范围，也不改变当前版本、`DONE`、未删除等检索硬过滤；这些约束仍由缓存未命中的检索 SQL 保证。
- 日志不得记录问题、回答、文档正文、引用内容、对象路径、JWT、缓存键、会话消息、用户 ID、知识库 ID或文档 ID。
- Redis 内部按用户 ID 存放统计属于业务存储，不得把该 ID 转为日志或指标标签。
- 参考文件中的“日志打印问题前 30 字符”和“日志打印缓存 key”不适用于本项目。

## 11. 测试计划

### 11.1 查询缓存服务

- 问题仅去除首尾空白，内部空白、大小写和标点变化产生不同键。
- 相同 `kb_ids` 的不同顺序产生相同键，范围不同产生不同键。
- Redis key 使用 MD5 摘要，不包含原始问题。
- 缓存值可以完整恢复答案、引用和命中数量，但不恢复历史耗时。
- TTL 使用配置值。
- 脏值、读失败按未命中降级；写失败不抛给问答调用方。
- 拒答、空引用和失败结果不写缓存。

### 11.2 权限与资格

- 缓存存在时仍先校验全部 `kb_ids` 权限。
- 任一知识库无权时返回 `403`，且没有 Redis 缓存读取。
- 当前用户身份或权限数据源不可用时返回 `503`，且没有缓存读取。
- 新会话和空会话可命中缓存。
- 存在一条或多条历史消息时既不读也不写缓存。
- 不同有权用户可以复用相同首轮缓存；权限回收后不能再读取。

### 11.3 同步与普通 RAG

- `POST /api/v1/rag/query` 首次未命中执行一次 RAG 并写缓存，第二次跳过 RAG。
- `POST /chat` 首轮命中后仍保存一对消息和引用，生成 Token 为 `0`。
- 首轮未命中成功后写缓存，多轮追问始终走完整 RAG。
- 缓存命中返回本次实际完整耗时而不是缓存中的旧耗时或固定 `0`。
- 缓存命中不触发 Token 记录或忠实性抽样。

### 11.4 SSE

- 首轮命中按 `status -> token -> done` 返回，`token` 是完整缓存答案。
- 首个 `status` 仍包含新建会话 ID。
- `done` 返回缓存引用和本次实际 `latency_ms`。
- 命中后成功保存消息；保存失败不发送成功 `done`。
- 有历史时事件与现有完整生成链路一致，且不访问查询结果缓存。

### 11.5 Token 与成本

- Embedding 只对 provider miss usage 累计，缓存命中不累计。
- 离线索引没有当前用户时不写用户 Hash。
- Context 使用裁剪后正文 Token，生成使用 provider output usage。
- provider usage 缺失时不补写近似生成 Token。
- Redis 累计失败不影响问答。
- 三类单价分别参与正确计算，使用 `Decimal`，并输出四位小数字符串和固定 `CNY`。
- 无历史统计返回全零；Redis 读取失败返回 `503`。
- 统计接口只能返回当前用户数据。

### 11.6 日志与回归

- 缓存命中、未命中和 Redis 失败日志不包含问题、缓存键、用户或知识库标识。
- 既有 V4 权限、Reranker 降级、引用、拒答和历史注入测试继续通过。
- 不新增 Token、成本或缓存 Prometheus 指标。

## 12. 推荐实施顺序

1. 为 `QueryCacheService` 和缓存 Schema 编写纯服务测试，实现键、序列化、TTL 与 Redis 降级。
2. 在普通 RAG 路由和同步聊天编排器中显式接入共享缓存服务，并保持 V4 服务职责不变。
3. 接入 SSE 首轮命中行为，补消息持久化、事件顺序与取消场景测试。
4. 扩展 `TokenMetrics` 的当前用户读取能力，并新增成本计算服务、Schema 与 `/stats/tokens` 路由。
5. 增加三个人民币单价配置及 `.env.example` 说明。
6. 完成权限、隐私、Redis 故障和全量回归验证。
7. 实施完成后补充对应 process 文档；本 Plan 阶段不创建实现过程记录。

## 13. 验收标准

- 相同首轮问题和知识库范围在 10 分钟内可以跨有权用户复用成功回答，且每次命中前均重新鉴权。
- 多轮追问继续携带最近五轮历史并始终绕过查询结果缓存。
- 缓存只保存成功且有引用的回答；拒答和失败不会延长内容不可用状态。
- 文档变更后的旧答案最多保留到 TTL 到期，该暂时不一致是明确接受的行为。
- 同步和 SSE 缓存命中均保存会话消息、返回引用和本次实际完整服务耗时。
- Redis 缓存或统计写失败不影响正常问答；统计 API 读取 Redis 失败明确返回 `503`。
- 当前用户可以查看三类累计 Token、总量、四位小数人民币估算成本和固定 `CNY` 币种。
- 离线索引、Reranker、完整 Prompt 输入和无 usage 生成不被伪装成精确用户成本。
- 日志与指标不泄露问题、回答、缓存键或高基数敏感标识。
- 不新增本模块的 Prometheus/Grafana 监控能力。

## 14. 验证命令

实施完成后至少执行：

```powershell
uv run pytest tests/services/test_query_cache.py -v
uv run pytest tests/services/test_token_metrics.py tests/services/test_cost_stats.py -v
uv run pytest tests/services/test_synchronous_chat.py tests/services/test_streaming_chat.py -v
uv run pytest tests/api/test_rag.py tests/api/test_chat.py tests/api/test_stats.py -v
uv run pytest -v
uv run ruff check .
uv run mypy app
```

测试文件名称可按实施时的现有结构微调。未配置或未运行的验证必须如实说明，不能以设计文档完成代替运行行为验证。
