# Prometheus/Grafana 监控

## Compose 部署

在仓库根目录准备生产环境变量后启动：

```bash
cp .env.production.example .env
docker compose up -d --build
```

首次创建 PostgreSQL 数据卷时，Compose 会自动执行 `app/db/ragkb_init.sql`，导入 PGVector 扩展、业务表、索引、触发器和当前演示数据。该脚本只适用于首次初始化；重复执行会因对象已存在而报错。

前端通过 Nginx 暴露在 80 端口，API 请求和 SSE 请求代理到 backend。Prometheus 和 Grafana 端口仅绑定到服务器本机，可通过 SSH 隧道访问：

```bash
ssh -L 3000:127.0.0.1:3000 -L 9090:127.0.0.1:9090 user@server
```

本目录提供 Token 监控的 Prometheus 抓取配置、告警规则和 Grafana Dashboard provisioning。共享服务器低资源部署使用 `compose.prod.yaml` 启动 Prometheus 和 Grafana；两者只绑定 `127.0.0.1`，Prometheus 数据保留 7 天或 256MB，访问时通过 SSH 隧道：

```bash
ssh -N -L 3000:127.0.0.1:3000 root@47.238.194.79
```

然后在本地浏览器打开 `http://localhost:3000`。Grafana 管理账号读取服务器 `/opt/rag-kb/.env` 中的 `GRAFANA_ADMIN_USER` 和 `GRAFANA_ADMIN_PASSWORD`。

部署时应满足：

- 应用设置 `ENABLE_METRICS=true`，Prometheus 抓取应用 `/metrics`。
- Prometheus 使用 `deploy/prometheus/prometheus.yml`，并挂载同目录下的 rules。
- Grafana 挂载 `deploy/grafana/provisioning` 和 `deploy/grafana/dashboards`。
- Grafana/Prometheus 的登录、反向代理和网络暴露只允许系统管理员与运维人员；本期不修改应用 `/metrics` 的网络 ACL。

Dashboard 的成本面板使用 `rag_token_usage_cost_cny_total`，表示按当前配置单价计算的估算值，不是供应商账单。
