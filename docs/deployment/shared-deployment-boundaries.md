# 共享部署平台边界与项目接入规则

> 当前已接入项目：Sales Agent、Resume Agent（`resume.shaoji.site`）、RAG-KB（`zhimai.shaoji.site`）。新项目必须按本文流程登记后接入。
> 当前服务器：`47.238.194.79`  
> 当前部署目录：`/opt/sales-agent`  
> 更新日期：2026-09-11

> 范围声明：这是服务器侧共享部署规则，不是本仓库的 Compose 实现规范。文中 `compose.resume-agent.yaml`、`resume-agent/` 和相关容器只存在于服务器或其他部署目录，当前仓库未提供这些文件；在本地开发和测试中请以根目录 `compose.yaml`、`README.md` 和 `docs/deployment/` 为准。

本文定义共享部署平台的边界。目标是让项目可以独立发布私有业务组件，同时由平台统一管理公网入口、证书、网络、端口和共享 Nginx，避免第二个或后续项目互相覆盖配置。

本文优先级高于各项目自己的部署便笺；项目文档只能补充本项目细节，不能放宽本文的共享资源约束。本文描述的是服务器侧事实，不是某个项目本地 Compose 文件的完整实现。

## 1. 当前拓扑

当前线上部署由基础 Compose 文件和 Resume Agent 的覆盖文件共同组成：

```text
/opt/sales-agent/compose.yaml
/opt/sales-agent/compose.resume-agent.yaml
                    |
                    v
          sales-agent-frontend-1
          共享 Nginx、80/443、证书挂载
             |                    |
             |                    +--> /var/www/resume-agent
             |                         Resume Agent 静态前端
             |
             +--> sales-agent-backend-1
             |    Sales Agent 后端
             |
             +--> sales-agent-mysql-1
             |    Sales Agent MySQL
             |
             +--> sales-agent-resume-agent-1
                  Resume Agent FastAPI 代理 -> Dify Cloud
```

### 项目归属

| 资源 | Sales Agent | Resume Agent | 边界说明 |
| --- | --- | --- | --- |
| 基础目录 | `/opt/sales-agent` | `/opt/sales-agent/resume-agent` | Resume Agent 是同一 Compose 项目的覆盖服务 |
| Compose 文件 | `compose.yaml` | `compose.resume-agent.yaml` | 生产命令必须同时加载两者 |
| 后端服务 | `backend` | `resume-agent` | 容器和代码目录独立 |
| 前端服务 | `frontend` | 使用 `frontend` 中的独立 Nginx server 块 | 这是当前最重要的共享点 |
| 数据库 | `mysql`，持久卷由 Sales Agent 使用 | 不使用 MySQL；使用 `resume-agent-metrics` SQLite 卷 | 禁止跨项目复用数据库账号和表 |
| Docker 网络 | `app` / `sales-agent_app` | 加入同一网络以访问 Nginx 上游 | 网络共享不代表数据共享 |
| 公网端口 | 由 `frontend` 占用 `80`、`443` | 通过同一 `frontend` 暴露 | 禁止为 Resume Agent 额外开放 `8001` |
| 域名 | 原有 Sales Agent 域名 | `resume.shaoji.site` | Nginx 按 `server_name` 分流 |

### 共享平台登记表

所有项目接入前必须在服务器侧维护登记表。第三个项目不得直接复制现有项目的 Compose 或 Nginx 配置后上线。

| 项目 | 规范域名 | 兼容域名 | 服务名/上游 | 数据存储 | 负责人 |
| --- | --- | --- | --- | --- | --- |
| Sales Agent | `shuhang.shaoji.site` | `shaoji.site`, `www.shaoji.site` | `backend:8000` | `mysql_data` | 项目维护者 |
| Resume Agent | `resume.shaoji.site` | 无 | `resume-agent:8001` | `resume-agent-metrics` | 项目维护者 |
| RAG-KB | `zhimai.shaoji.site` | 无 | `rag-kb-backend:8000` | `rag-kb_db-data`, `rag-kb_redis-data`, `rag-kb_minio-data` | 项目维护者 |
| 新项目 | 接入前分配 | 接入前分配 | 接入前分配 | 接入前分配 | 必须填写 |

RAG-KB 使用独立 Compose 项目 `/opt/rag-kb`，私有服务为 PostgreSQL、Redis、MinIO 和 `rag-kb-backend`；只有 `rag-kb-backend` 加入 `sales-agent_app`。共享 Nginx 通过 `compose.rag-kb.yaml` 挂载 `deploy/nginx/zhimai.shaoji.site.conf` 和静态前端目录；`zhimai.shaoji.site` 使用独立 Let's Encrypt 证书，`/etc/cron.d/rag-kb-certbot` 每日续期并 reload 共享 Nginx。

当前使用的是 Docker Compose 方案。仓库中的 `deploy/resume-agent.service` 是备用的主机级 systemd 方案，不能与当前 Docker 版 Resume Agent 同时启动，也不能让两套方案同时监听 `8001`。

共享平台的唯一公网入口是 `sales-agent-frontend-1` 中的 Nginx。业务项目不单独占用宿主机 80/443，不在宿主机再安装第二个边缘 Nginx。

## 2. 共享组件与影响范围

以下资源由所有已登记项目共同依赖，任何修改都属于“公共部署变更”：

1. `sales-agent-frontend-1` 容器及其 Nginx 主进程。
2. 宿主机 `80`、`443` 端口和云安全组对应规则。
3. `/opt/sales-agent/certbot` 下的证书和 ACME webroot 挂载。
4. Docker 网络 `sales-agent_app`（Compose 服务名通常为 `app`）。
5. 服务器的 CPU、内存、磁盘和 Docker daemon。
6. `/opt/sales-agent/compose.yaml` 中影响 `frontend`、网络、卷或端口的配置。

共享证书 `www.shaoji.site` 必须同时覆盖 `shaoji.site`、`www.shaoji.site`、`shuhang.shaoji.site` 和 `resume.shaoji.site`。任何 Certbot 签发、替换或续期配置都必须保留这四个 SAN；只申请 Sales Agent 域名会导致 Resume Agent 的 HTTPS 校验失败。

### 共享资源所有权

| 资源 | 唯一管理边界 | 项目可做的事 | 禁止事项 |
| --- | --- | --- | --- |
| 宿主机 80/443 | 共享平台 | 提交自己的域名路由变更 | 项目自行绑定端口或启动第二个 Nginx |
| `frontend` Nginx | 共享平台 | 增加经审核的独立 `server`/`location` | 覆盖其他项目配置、删除未知挂载 |
| `certbot/conf`、`certbot/www` | 共享平台 | 申请/续期时保留全部 SAN | 为单项目重建共享证书并丢失其他域名 |
| `sales-agent_app` 网络 | 共享平台 | 使用已登记的服务名和端口 | 自行改网段、抢占固定 IP、暴露宿主机端口 |
| MySQL、业务卷、项目密钥 | 对应项目 | 仅访问自己的数据 | 跨项目共用卷、账号或密钥 |

固定网络当前为 `172.30.0.0/24`，`frontend` 保留 `172.30.0.2`。后续固定 IP 必须登记后分配；能使用服务名发现的组件不得依赖 IP。

以下资源原则上是项目私有的：

- Sales Agent 的 `backend` 镜像、业务代码、MySQL 数据和业务环境变量。
- Resume Agent 的 `resume-agent` 镜像、`web/` 静态文件、代理环境文件和 `resume-agent-metrics` 卷。
- Dify API Key、Resume Agent 会话密钥和监控令牌。密钥只允许存在服务器受限环境文件中。

## 3. 新项目接入流程

第三个及以后项目必须先完成登记和评审，再修改共享入口。接入顺序固定如下：

1. **划定私有资源**：确定项目目录、镜像、服务名、内部监听端口、数据卷、环境文件和密钥，确认不复用已有项目的数据卷、数据库账号或密钥。
2. **分配域名**：确定唯一规范域名和可选兼容域名；所有域名的 DNS、HTTPS 证书 SAN、CORS 来源和 Nginx `server_name` 必须一致。
3. **分配网络资源**：优先使用 Compose 服务名通信；确需固定 IP 时，在 `172.30.0.0/24` 登记未占用地址，禁止自行改变网段或使用已分配地址。
4. **准备覆盖配置**：项目专属 Compose 使用独立覆盖文件，不能把第三方服务直接写进另一个项目的基础文件。覆盖文件只能增加自己的服务、卷和 Nginx 片段，不得删除共享挂载或改写其他项目服务。
5. **预演合并配置**：在服务器执行完整 Compose 文件集合的 `config --quiet`、服务依赖检查和 `nginx -t`；检查最终配置而不是只检查单个项目文件。
6. **分阶段发布**：先发布项目私有后端和数据服务，再以共享入口变更单独评审 Nginx/证书，最后验证所有已登记项目的首页和健康检查。
7. **登记回滚信息**：记录新增服务、域名、卷、端口、证书 SAN、变更提交和回滚命令；回滚时不得删除其他项目的数据卷或覆盖其路由。

新项目接入完成的最低验收项：

```bash
docker compose -f compose.yaml -f compose.resume-agent.yaml -f compose.<new-project>.yaml config --quiet
docker exec sales-agent-frontend-1 nginx -t
curl -fsS https://<new-project-domain>/health
curl -fsS https://resume.shaoji.site/api/health
curl -fsS https://shuhang.shaoji.site/health
```

上面的 `compose.<new-project>.yaml` 仅表示服务器侧登记的覆盖文件名；本仓库不提供该文件。

## 4. 允许的独立变更

### Sales Agent 业务代码

修改 Sales Agent 的后端业务代码时，只重建 `backend`：

```bash
cd /opt/sales-agent
docker compose -f compose.yaml -f compose.resume-agent.yaml \
  up -d --build backend
```

不要因为后端发布而重建或删除 `frontend`、`resume-agent`、`mysql`。

### Resume Agent 业务代码

修改 Resume Agent 的 FastAPI 代理代码时，只重建 `resume-agent`：

```bash
cd /opt/sales-agent
docker compose -f compose.yaml -f compose.resume-agent.yaml \
  up -d --build resume-agent
```

修改 Resume Agent 的静态前端时，更新以下目录中的文件：

```text
/opt/sales-agent/resume-agent/web/
```

静态文件通过只读挂载进入共享 `frontend` 容器，通常不需要重建镜像；更新后执行 Nginx 配置检查和 reload：

```bash
docker exec sales-agent-frontend-1 nginx -t
docker exec sales-agent-frontend-1 nginx -s reload
```

## 5. 必须协同的公共变更

以下改动不能作为单个项目的普通发布处理，必须先检查所有已登记项目：

- 修改 `compose.yaml` 或 `compose.resume-agent.yaml` 中的 `frontend`、端口、网络、证书或卷配置。
- 修改 Nginx 主配置、`server_name`、TLS、共享限流区或任一站点的 `location`。
- 重建、替换或迁移 `sales-agent-frontend-1`。
- 更新证书、Certbot 挂载路径或 ACME webroot。
- 修改 Docker 网络、Docker daemon、宿主机防火墙或云安全组。
- 修改基础 Compose 的固定网络网段或 `frontend` 静态 IP；必须先确认覆盖文件没有地址冲突。
- 调整服务器内存、CPU、磁盘配额，或执行会清理 Docker 资源的操作。
- 变更 `/opt/sales-agent` 目录结构或移动 `resume-agent` 子目录。

公共变更至少要完成以下步骤：

```bash
cd /opt/sales-agent
docker compose -f compose.yaml -f compose.resume-agent.yaml config --quiet
docker compose -f compose.yaml -f compose.resume-agent.yaml ps
docker exec sales-agent-frontend-1 nginx -t
```

确认配置无误后，再按影响范围重建服务。Nginx reload 后必须分别验证所有已登记项目的首页或健康检查；当前至少验证：

```bash
curl -fsS https://resume.shaoji.site/api/health
```

## 6. 禁止操作

### 不得遗漏覆盖文件

当前 `frontend` 的 Resume Agent 挂载和 `resume-agent` 服务都来自覆盖文件。以下命令会以基础文件为准，可能导致 Resume Agent 挂载、依赖关系或服务状态丢失：

```bash
docker compose up -d --build frontend
```

基础文件单独执行 `docker compose restart frontend` 不会删除现有挂载，但会让所有项目共用的公网入口短暂中断，因此也不作为日常发布命令。

涉及本项目时，始终使用：

```bash
docker compose -f compose.yaml -f compose.resume-agent.yaml \
  up -d --build frontend
```

### 不得随意销毁共享资源

未经共享平台维护者和所有受影响项目确认，不执行：

```bash
docker compose down
docker compose down -v
docker system prune
docker volume prune
```

特别是 `down -v` 可能删除持久卷；任何数据库或统计数据丢失都不能通过重新构建镜像恢复。

### 不得重复部署第二套入口

- 不在宿主机另装第二个 Nginx。
- 不启动备用 systemd 服务 `resume-agent.service`，除非已经停止并移除 Docker 版服务且完成单独迁移方案。
- 不新增公网 `8001`、`3306` 或前端开发端口 `5173`。
- 不将 Dify API Key 放进 `web/app.js`、HTML、Nginx 配置或镜像层。

## 7. 发布前后检查清单

### 发布前

- [ ] 明确本次变更属于项目私有变更还是公共部署变更，并通知所有受影响项目负责人。
- [ ] 记录当前容器状态：`docker compose -f compose.yaml -f compose.resume-agent.yaml ps`。
- [ ] 检查磁盘空间和容器资源，确认不会因构建或日志导致磁盘耗尽。
- [ ] 若涉及 MySQL，先完成 Sales Agent 数据库备份。
- [ ] 若涉及 Nginx 或证书，先保留当前配置文件副本。
- [ ] 确认没有并行发布正在重建 `frontend`、修改证书或变更共享网络。

### 发布后

- [ ] `docker compose ... ps` 中 Sales Agent 和 Resume Agent 服务均处于预期状态。
- [ ] `docker exec sales-agent-frontend-1 nginx -t` 成功。
- [ ] Sales Agent 首页、业务接口和数据库连接通过已有验收项。
- [ ] `https://resume.shaoji.site/` 返回 `200`。
- [ ] `https://resume.shaoji.site/api/health` 返回 `{"status":"ready"}`。
- [ ] Resume Agent 至少完成一次流式聊天；确认 Dify 状态标记和正式回答显示正常。
- [ ] `ss -ltn` 确认公网没有暴露 `3306`、`8001`、`5173`。

## 8. 回滚边界

### 静态前端回滚

只回滚 Resume Agent 静态文件，不重启 Sales Agent 后端或 MySQL。部署前应在服务器保留备份，例如：

```bash
cd /opt/sales-agent/resume-agent
tar -czf web-backup-$(date +%Y%m%d-%H%M%S).tar.gz web/app.js web/styles.css web/index.html
```

恢复后仅需重新执行 Nginx 检查和 reload。

### Resume Agent 代理回滚

使用上一版本代码重新构建 `resume-agent`，不要删除 `resume-agent-metrics` 卷：

```bash
cd /opt/sales-agent
docker compose -f compose.yaml -f compose.resume-agent.yaml \
  up -d --build resume-agent
```

### 公共 Nginx 回滚

公共 Nginx 回滚前，必须同时检查两个域名的配置。先恢复对应配置副本，再执行：

```bash
docker exec sales-agent-frontend-1 nginx -t
docker exec sales-agent-frontend-1 nginx -s reload
```

如果 `nginx -t` 失败，不要 reload；保留当前运行中的 Nginx 进程，并修复或恢复配置后再操作。

## 9. 故障判断顺序

1. 先判断是单项目故障还是共享入口故障：检查所有已登记域名是否同时异常。
2. 检查 `frontend` 容器和 Nginx 配置；多个项目同时异常时优先看这里。
3. 仅 Resume Agent 异常时检查 `resume-agent` 容器、`/api/health` 和 Dify 上游连接。
4. 仅 Sales Agent API 异常时检查 `backend` 和 MySQL，不要重启 Resume Agent。
5. 出现超时、502 或容器反复重启时检查 CPU、内存、磁盘和 Docker 日志。
6. 未确认数据安全前，不删除容器、卷、证书目录或日志。

## 10. 长期解耦建议

当前多个项目共享 `frontend` Nginx 容器，这是可运行但仍存在部署耦合的方案。后续若需要完全独立发布，优先考虑：

- 为 Resume Agent 使用独立的反向代理或独立前端容器，再由一层稳定的入口按域名转发；或
- 将各项目迁移到明确的独立 Compose 项目，并保留唯一的、经过审查的边缘 Nginx。

在完成迁移、证书切换、回滚演练和公网验收前，不要直接删除当前共享 `frontend` 方案。
