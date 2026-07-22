# 06 — 交付 Prometheus/Grafana 监控面板与规则

**What to build:** 将六类 Token 指标接入可采集的 Prometheus 监控链路，并提供 Grafana 面板和告警规则。运维人员可以按模型、Token 类型和知识库范围查看 Token 速率、日增量、成本估算、预算状态和异常观测；告警本期只展示状态，不发送外部通知。

**Blocked by:** 01 — 建立六类 Token 统计契约与最终回答路径; 02 — 接入 Embedding Token 统计; 03 — 接入 HyDE 与忠实性检测 Token 统计; 04 — 接入 Reranker Token 统计; 05 — 增加全局 Token 预算闸门与单次异常告警

**Status:** resolved

- [ ] Prometheus 能从现有 `/metrics` 采集核心 Token Counter、usage unavailable、预算使用量、预算拒绝、单次超限和 sink 写入失败指标。
- [ ] 核心 Token 指标仅使用 `model`、`token_type`、`kb_id` 标签；多知识库请求为 `multi`，不出现用户、部门、请求、问题、回答、文档或对象路径标签。
- [ ] 提供总览面板，展示当天 Token 总量、5 分钟速率、估算成本、预算使用比例和预算拒绝次数。
- [ ] 提供按 Token 类型、模型和知识库筛选的速率、小时增量、日增量和阶段成本面板。
- [ ] 提供 usage 缺失、单次超过 20,000、预算 80%/100%和统计 sink 故障状态面板。
- [ ] Prometheus recording/alert rules 能产生 Warning/Critical 状态；Grafana 使用 Prometheus 数据源完成面板查询。
- [ ] 成本展示使用当前配置单价并明确标记为估算，不作为供应商账单。
- [ ] Grafana/Prometheus 查询权限仅授予系统管理员和运维人员，不新增 `/metrics` 网络 ACL。
- [ ] 验证监控配置、指标暴露、PromQL 规则、Dashboard provisioning 和隐私标签边界。
