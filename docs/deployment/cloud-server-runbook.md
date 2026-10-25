# 云服务器上线运行手册（域名共享部署，历史方案）

> 历史说明：2026-10-26 起，生产站点已迁移到新服务器 `123.206.117.213`，改为仅 IP 部署。
> 当前生产拓扑与操作步骤以 `docs/deployment/ip-single-server.md` 和 `deploy/ip/README.md` 为准。
> 本文保留旧服务器 `47.238.194.79` 的域名共享部署记录，仅用于历史排查和回滚，不要按本文操作当前生产环境。

本文整理当前项目已验证过的上线流程，适用于单台 Linux 云服务器、Docker Compose、PostgreSQL/PGVector、Redis、MinIO、Nginx 和 Let’s Encrypt。

> 共享服务器说明：`47.238.194.79` 上的 Portal（`shaoji.site`）、Resume Agent（`resume.shaoji.site`）和 RAG-KB（`zhimai.shaoji.site`）共用 `frontend`、80/443、证书目录和 Docker 网络。服务器侧 Compose 必须同时加载 `compose.resume-agent.yaml`、`compose.rag-kb.yaml` 和 `compose.portal.yaml`。这些服务器侧覆盖文件不在本仓库中；RAG-KB 自身的生产配置以 `compose.prod.yaml` 为准。数航 Agent 已于 2026-10-25 下线，`shuhang.shaoji.site` 仅保留 `410` 响应。

## 1. 当前生产拓扑

```text
浏览器
  -> 云安全组
  -> 共享 Nginx（80/443）
       -> shaoji.site             Portal 静态页
       -> resume.shaoji.site      Resume Agent -> Dify Cloud
       -> zhimai.shaoji.site      RAG-KB -> PostgreSQL / Redis / MinIO
```

RAG-KB 的运行边界：

- `rag-kb-backend` 加入共享 `sales-agent_app` 网络，供共享 Nginx 反向代理。
- PostgreSQL、Redis、MinIO 只运行在 `/opt/rag-kb` 的独立 Compose 项目内。
- `5432`、`6379`、`9000`、`8000` 不对公网开放。
- `.env` 只保存在服务器，不提交 Git。

## 2. SSH 与安全组

安全组只开放必要端口：

| 端口 | 协议 | 来源 | 用途 |
| --- | --- | --- | --- |
| `22` | TCP | 个人固定 IP 更佳 | SSH 运维 |
| `80` | TCP | `0.0.0.0/0` | HTTP 跳转和 ACME 验证 |
| `443` | TCP | `0.0.0.0/0` | HTTPS 网站 |

不要开放 `5432`、`6379`、`9000`、`8000` 或 `5173`。

SSH 免密检查：

```powershell
Get-ChildItem $env:USERPROFILE\.ssh
ssh -o BatchMode=yes -o PasswordAuthentication=no root@47.238.194.79 "id -un && hostname"
```

不要把服务器密码、私钥或 `.env` 写入文档、命令历史或 Git。

## 3. DNS

DNS A 记录：

| 主机记录 | 类型 | 记录值 | 状态 |
| --- | --- | --- | --- |
| `@` | A | `47.238.194.79` | Portal |
| `www` | A | `47.238.194.79` | 跳转 Portal |
| `resume` | A | `47.238.194.79` | Resume Agent |
| `zhimai` | A | `47.238.194.79` | RAG-KB |
| `shuhang` | A | 已退役 | 可在证书到期前删除 |

检查 DNS：

```powershell
Resolve-DnsName shaoji.site -Type A
Resolve-DnsName resume.shaoji.site -Type A
Resolve-DnsName zhimai.shaoji.site -Type A
```

## 4. 安装 Docker 并同步代码

```bash
docker --version
docker compose version
```

RAG-KB 部署目录：

```bash
mkdir -p /opt/rag-kb
cd /opt/rag-kb
git clone <repository-url> .
git checkout <commit-or-tag>
```

发布前在本地提交并推送，服务器拉取固定提交，确保可追踪、可回滚。

## 5. 生产配置

```bash
cd /opt/rag-kb
cp .env.production.example .env
chmod 600 .env
```

至少确认：

```env
APP_ENV=production
DATABASE_URL=postgresql+asyncpg://ragkb:<db-password>@db:5432/ragkb
SYNC_DATABASE_URL=postgresql+psycopg://ragkb:<db-password>@db:5432/ragkb
DB_ECHO=false
SECRET_KEY=<random-secret-at-least-32-bytes>
OPENAI_API_KEY=<deepseek-key>
OPENAI_BASE_URL=https://api.deepseek.com
CHAT_MODEL=deepseek-flash
EMBEDDING_API_KEY=<dashscope-key>
EMBEDDING_BASE_URL=https://dashscope.aliyuncs.com/compatible-mode/v1
RERANKER_API_KEY=<reranker-key>
RERANKER_ENDPOINT=<reranker-endpoint>
```

关键规则：

- 数据库主机名使用 Compose 服务名 `db`，不要写宿主机 `127.0.0.1`。
- Chat 使用 DeepSeek；Embedding 和 Reranker 继续使用阿里云百炼，不要把 DeepSeek 密钥复用到后两者。
- `.env`、证书私钥和数据库备份不能提交 Git。
- PostgreSQL、Redis、MinIO 的数据卷不能与其它项目共用。

检查 RAG-KB Compose：

```bash
docker compose -f compose.prod.yaml config --quiet
```

检查共享入口合并配置：

```bash
cd /opt/sales-agent
docker compose -f compose.yaml -f compose.resume-agent.yaml \
  -f compose.rag-kb.yaml -f compose.portal.yaml config --quiet
```

## 6. 启动与更新

RAG-KB：

```bash
cd /opt/rag-kb
docker compose -f compose.prod.yaml up -d --build rag-kb-backend
```

共享入口：

```bash
cd /opt/sales-agent
docker compose -f compose.yaml -f compose.resume-agent.yaml \
  -f compose.rag-kb.yaml -f compose.portal.yaml up -d --no-deps frontend
docker exec sales-agent-frontend-1 nginx -t
docker exec sales-agent-frontend-1 nginx -s reload
```

不得单独执行 `docker compose down -v` 或删除共享数据卷。

## 7. HTTPS 证书

当前证书：

- `www.shaoji.site`：覆盖 `shaoji.site`、`www.shaoji.site`、`shuhang.shaoji.site`，由 Portal 和共享入口使用。
- `resume.shaoji.site`：Resume Agent 专用。
- `zhimai.shaoji.site`：RAG-KB 专用。

证书目录在 `/opt/sales-agent/certbot/conf`，ACME webroot 为 `/opt/sales-agent/certbot/www`。

续期检查：

```bash
docker run --rm \
  -v /opt/sales-agent/certbot/conf:/etc/letsencrypt \
  -v /opt/sales-agent/certbot/www:/var/www/certbot \
  certbot/certbot renew --dry-run --cert-name www.shaoji.site
```

`shuhang` 已退役，但当前证书仍包含该 SAN。删除该 DNS 或从证书 SAN 移除前，先确认不会影响 Portal 和 Resume 的证书续期。

## 8. 备份

RAG-KB 数据库备份：

```bash
mkdir -p /opt/rag-kb/backups
chmod 700 /opt/rag-kb/backups
docker exec rag-kb-db-1 sh -c \
  'pg_dump -U "$POSTGRES_USER" "$POSTGRES_DB"' \
  > /opt/rag-kb/backups/ragkb-$(date +%Y%m%d-%H%M%S).sql
chmod 600 /opt/rag-kb/backups/*.sql
```

MinIO 原始文档和 PostgreSQL 数据必须分别备份；不能只备份代码或镜像。

## 9. 公网验收

服务器本机：

```bash
docker compose -f /opt/rag-kb/compose.prod.yaml ps
docker exec sales-agent-frontend-1 nginx -t
curl -fsS https://shaoji.site/
curl -fsS https://resume.shaoji.site/api/health
curl -fsS https://zhimai.shaoji.site/api/v1/health/ready
curl -sS -o /dev/null -w '%{http_code}\n' https://shuhang.shaoji.site/
```

本机公网：

```powershell
curl.exe -I http://shaoji.site
curl.exe -I https://shaoji.site
curl.exe https://resume.shaoji.site/api/health
curl.exe https://zhimai.shaoji.site/api/v1/health/ready
```

验收标准：

- Portal、Resume、RAG-KB 首页返回 `200`。
- Resume `/api/health` 返回 `ready`。
- RAG-KB `/api/v1/health/ready` 返回 `READY`，数据库、Redis、MinIO 均为 `true`。
- `shuhang.shaoji.site` 返回 `410`。
- PostgreSQL、Redis、MinIO 容器为 `healthy`。
- 公网未暴露 `5432`、`6379`、`9000`、`8000`。

## 10. 常见故障、更新和回滚

| 现象 | 优先检查 |
| --- | --- |
| 公网 80/443 超时 | 安全组、防火墙、`ss -ltn`、DNS |
| 多个域名同时异常 | 共享 `frontend`、Nginx 配置、宿主机资源 |
| Resume 异常 | `resume-agent` 容器、`/api/health`、Dify 上游 |
| RAG-KB 异常 | `rag-kb-backend`、PostgreSQL、Redis、MinIO |
| RAG-KB 502 | 共享网络、服务名、后端健康状态 |
| Certbot 失败 | DNS、80 端口、ACME webroot、证书 SAN |

更新前记录：

```bash
git -C /opt/rag-kb rev-parse HEAD
docker compose -f /opt/rag-kb/compose.prod.yaml ps
docker compose -f /opt/sales-agent/compose.yaml \
  -f /opt/sales-agent/compose.resume-agent.yaml \
  -f /opt/sales-agent/compose.rag-kb.yaml \
  -f /opt/sales-agent/compose.portal.yaml ps
```

回滚时恢复固定提交和配置副本，先做 `config --quiet` 与 `nginx -t`，再分项目重启。数据库迁移前必须备份，并保留回滚方案。
