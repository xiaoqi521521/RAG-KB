# Prometheus/Grafana 监控

## Compose 部署

在仓库根目录准备生产环境变量后启动：

```bash
cp .env.production.example .env
docker compose up -d --build
```

首次创建 PostgreSQL 数据卷时，Compose 会依次执行 `00-bootstrap.sql`、由 `01-ragkb-full-dump.sql` 引入的 `app/db/ragkb_full_dump.sql` 和 `99-sequence-sync.sql`。原始 dump 挂载在 `/tmp`，不放入 PostgreSQL initdb 目录，确保只导入一次。该导出文件包含历史结构和数据；后续数据库变更需要同步维护导入脚本。

前端通过 Nginx 暴露在 80 端口，API 请求和 SSE 请求代理到 backend。Prometheus 和 Grafana 端口仅绑定到服务器本机，可通过 SSH 隧道访问：

```bash
ssh -L 3000:127.0.0.1:3000 -L 9090:127.0.0.1:9090 user@server
```

本目录提供 Token 监控的 Prometheus 抓取配置、告警规则和 Grafana Dashboard provisioning。

部署时应满足：

- 应用设置 `ENABLE_METRICS=true`，Prometheus 抓取应用 `/metrics`。
- Prometheus 使用 `deploy/prometheus/prometheus.yml`，并挂载同目录下的 rules。
- Grafana 挂载 `deploy/grafana/provisioning` 和 `deploy/grafana/dashboards`。
- Grafana/Prometheus 的登录、反向代理和网络暴露只允许系统管理员与运维人员；本期不修改应用 `/metrics` 的网络 ACL。

Dashboard 的成本面板使用 `rag_token_usage_cost_cny_total`，表示按当前配置单价计算的估算值，不是供应商账单。
