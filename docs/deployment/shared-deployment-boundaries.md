# 共享部署平台边界与项目接入规则

> 当前已接入项目：Portal（`shaoji.site`）、Resume Agent（`resume.shaoji.site`）、RAG-KB（`zhimai.shaoji.site`）。原数航 Agent（`shuhang.shaoji.site`）已于 2026-10-25 下线，域名保留 `410` 响应。新项目必须按本文流程登记后接入。
> 当前服务器：`47.238.194.79`
> 当前部署目录：`/opt/sales-agent`
> 更新日期：2026-10-25

> 范围声明：这是服务器侧共享部署规则，不是本仓库的 Compose 实现规范。文中 `compose.resume-agent.yaml`、`compose.rag-kb.yaml`、`compose.portal.yaml` 和 `resume-agent/` 只存在于服务器；当前仓库仅提供 RAG-KB 的 Compose 与文档。

本文定义共享部署平台的边界。目标是让项目可以独立发布私有业务组件，同时由平台统一管理公网入口、证书、网络、端口和共享 Nginx，避免项目之间互相覆盖配置。

## 1. 当前拓扑

```text
/opt/sales-agent/compose.yaml                 # 纯共享 Nginx，无业务后端
/opt/sales-agent/compose.resume-agent.yaml    # Resume Agent
/opt/sales-agent/compose.rag-kb.yaml          # RAG-KB 路由与静态文件挂载
/opt/sales-agent/compose.portal.yaml          # Portal 静态页
                    |
                    v
          sales-agent-frontend-1
          共享 Nginx、80/443、证书挂载
             |                    |
             |                    +--> /var/www/portal        Portal 静态页
             |                    +--> /var/www/resume-agent   Resume Agent 静态页
             |                    +--> /var/www/rag-kb         RAG-KB 静态页
             |
             +--> resume-agent:8001       Resume Agent 代理 -> Dify Cloud
             +--> rag-kb-backend:8000     RAG-KB FastAPI（独立 Compose 项目）
```

### 项目归属

| 资源 | Portal | Resume Agent | RAG-KB |
| --- | --- | --- | --- |
| 目录 | `/opt/shaoji-portal` | `/opt/sales-agent/resume-agent` | `/opt/rag-kb` |
| Compose | `compose.portal.yaml` | `compose.resume-agent.yaml` | `compose.prod.yaml` |
| 公网域名 | `shaoji.site`、`www.shaoji.site` | `resume.shaoji.site` | `zhimai.shaoji.site` |
| 数据 | 无 | `resume-agent-metrics` | PostgreSQL、Redis、MinIO 三卷 |
| 网络 | 只挂静态目录 | `sales-agent_app` | 后端加入 `sales-agent_app` |

共享平台登记表：

| 项目 | 规范域名 | 兼容域名 | 服务名/上游 | 数据存储 |
| --- | --- | --- | --- | --- |
| Portal | `shaoji.site` | `www.shaoji.site` | 静态 `/var/www/portal` | 无 |
| Resume Agent | `resume.shaoji.site` | 无 | `resume-agent:8001` | `resume-agent-metrics` |
| RAG-KB | `zhimai.shaoji.site` | 无 | `rag-kb-backend:8000` | `rag-kb_db-data`、`rag-kb_redis-data`、`rag-kb_minio-data` |

已退役的 `shuhang.shaoji.site` 不再属于任何项目。其 80/443 请求返回 `410`，不再引用业务后端；证书 SAN、DNS 可在证书到期前清理。不要为它恢复任何 Compose 服务或静态目录。

共享平台的唯一公网入口是 `sales-agent-frontend-1`。业务项目不单独占用宿主机 80/443。

## 2. 共享组件与影响范围

所有已登记项目共同依赖：

1. `sales-agent-frontend-1` 及其 Nginx 进程。
2. 宿主机 `80`、`443` 和云安全组规则。
3. `/opt/sales-agent/certbot` 下的证书与 ACME webroot。
4. Docker 网络 `sales-agent_app`。
5. `/opt/sales-agent/compose.yaml`、各项目覆盖文件与共享 Nginx 配置。

以下资源属于项目私有：

- Resume Agent 的镜像、`resume-agent/` 源码与 `resume-agent-metrics` 卷。
- RAG-KB 的 `/opt/rag-kb`、`.env`、PostgreSQL / Redis / MinIO 数据卷。
- Portal 的 `/opt/shaoji-portal/web` 静态文件。

## 3. 新项目接入流程

1. 确定私有目录、镜像、服务名、内部端口、数据卷和环境文件，禁止复用已有项目数据。
2. 分配唯一规范域名，DNS 与证书 SAN 保持一致。
3. 优先通过 `sales-agent_app` 和 Compose 服务名通信，不依赖固定 IP。
4. 使用独立覆盖文件，只增加自己的服务、卷和 Nginx 片段，不修改其他项目服务。
5. 在服务器执行完整 Compose 文件集合的 `config --quiet` 和 `nginx -t`。
6. 先发布私有后端和数据服务，再单独变更共享入口，最后验证所有已登记项目。
7. 记录回滚命令和备份位置。

当前生产命令至少加载：

```bash
cd /opt/sales-agent
docker compose -f compose.yaml -f compose.resume-agent.yaml \
  -f compose.rag-kb.yaml -f compose.portal.yaml config --quiet
docker compose -f compose.yaml -f compose.resume-agent.yaml \
  -f compose.rag-kb.yaml -f compose.portal.yaml ps
docker exec sales-agent-frontend-1 nginx -t
```

## 4. 项目独立发布

Resume Agent：

```bash
cd /opt/sales-agent
docker compose -f compose.yaml -f compose.resume-agent.yaml \
  up -d --build resume-agent
```

RAG-KB：

```bash
cd /opt/rag-kb
docker compose -f compose.prod.yaml up -d --build rag-kb-backend
```

Portal 或 Resume 静态页更新后，只需检查并 reload 共享 Nginx：

```bash
docker exec sales-agent-frontend-1 nginx -t
docker exec sales-agent-frontend-1 nginx -s reload
```

## 5. 公共变更

以下改动必须先检查全部已登记项目：

- 修改共享 `frontend`、端口、网络、证书或卷。
- 修改任一 `server_name`、TLS、限流区或 `location`。
- 重建或替换 `sales-agent-frontend-1`。
- 更新证书、Certbot 挂载或 ACME webroot。
- 调整 Docker 网络、宿主机防火墙或云安全组。

发布后至少验证：

```bash
curl -fsS https://shaoji.site/
curl -fsS https://resume.shaoji.site/api/health
curl -fsS https://zhimai.shaoji.site/api/v1/health/ready
```

## 6. 禁止操作

执行共享操作时必须加载全部覆盖文件。以下命令不得用于生产：

```bash
docker compose up -d --build frontend
docker compose down
docker compose down -v
docker system prune
docker volume prune
```

不得：

- 在宿主机另装第二个 Nginx。
- 启动备用 `resume-agent.service`，除非先移除 Docker 版并完成迁移。
- 新开公网 `8001`、`3306`、`5173`。
- 把 Dify API Key、数据库密码写进静态文件或镜像层。
- 恢复已退役的 `shuhang` 路由、服务或数据卷。

## 7. 发布前后检查

发布前：

- [ ] 确认变更属于项目私有还是共享入口。
- [ ] 记录容器状态和当前 Nginx 配置。
- [ ] 检查磁盘和资源余量。
- [ ] 涉及 RAG-KB 数据或 Resume 数据时先完成备份。
- [ ] 确认没有并行发布正在重建共享入口。

发布后：

- [ ] `docker compose ... ps` 服务状态符合预期。
- [ ] `nginx -t` 成功后再 reload。
- [ ] `https://shaoji.site/` 返回 `200`。
- [ ] `https://resume.shaoji.site/api/health` 返回 `ready`。
- [ ] `https://zhimai.shaoji.site/api/v1/health/ready` 返回 `READY`。
- [ ] `https://shuhang.shaoji.site/` 返回 `410`。
- [ ] 公网未暴露 `3306`、`8001`、`5173`。

## 8. 回滚边界

Resume 静态页回滚只恢复 `/opt/sales-agent/resume-agent/web/`，然后 `nginx -t`、reload。

Resume 代理回滚重建 `resume-agent`，不要删除 `resume-agent-metrics`。

RAG-KB 回滚只操作 `/opt/rag-kb/compose.prod.yaml` 和 RAG-KB 后端，不要删除其 PostgreSQL、Redis、MinIO 卷。

共享 Nginx 回滚前先恢复配置副本，确认 `nginx -t` 成功后再 reload；失败时保留当前进程。

## 9. 故障判断顺序

1. 多个域名同时异常：先检查 `sales-agent-frontend-1`、Nginx 和宿主机 80/443。
2. 仅 Resume 异常：检查 `resume-agent` 与 `/api/health`。
3. 仅 RAG-KB 异常：检查 `rag-kb-rag-kb-backend-1` 与 `/api/v1/health/ready`。
4. 超时、502 或重启：检查 CPU、内存、磁盘和容器日志。
5. 未确认数据安全前，不删除容器、卷、证书或日志。

## 10. 长期解耦建议

当前共享 `frontend` 仍是单点。后续可为各项目拆分独立入口，再由稳定边缘 Nginx 按域名转发；迁移和验收完成前，不要直接删除现有共享入口。