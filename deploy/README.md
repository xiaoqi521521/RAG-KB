# Prometheus/Grafana 监控

本目录提供 Token 监控的 Prometheus 抓取配置、告警规则和 Grafana Dashboard provisioning。

部署时应满足：

- 应用设置 `ENABLE_METRICS=true`，Prometheus 抓取应用 `/metrics`。
- Prometheus 使用 `deploy/prometheus/prometheus.yml`，并挂载同目录下的 rules。
- Grafana 挂载 `deploy/grafana/provisioning` 和 `deploy/grafana/dashboards`。
- Grafana/Prometheus 的登录、反向代理和网络暴露只允许系统管理员与运维人员；本期不修改应用 `/metrics` 的网络 ACL。

Dashboard 的成本面板使用 `rag_token_usage_cost_cny_total`，表示按当前配置单价计算的估算值，不是供应商账单。
