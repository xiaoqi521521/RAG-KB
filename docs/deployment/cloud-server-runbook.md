# 云服务器上线运行手册

本文整理本项目已验证过的上线流程，适用于单台 Linux 云服务器、Docker Compose、MySQL、Nginx 和 Let’s Encrypt。后续项目可以复用步骤，但必须替换项目名、域名、数据库名和密钥。

> 共享服务器特别说明：`47.238.194.79` 上的 Sales Agent 与 Resume Agent 共用 `frontend`、80/443、证书目录和 Docker 网络。该环境的所有 Compose 操作必须同时加载服务器侧的 `compose.resume-agent.yaml` 覆盖文件；本仓库未包含该文件，下面涉及生产 Compose 的命令均按共享部署方式书写。若是完全隔离的单项目环境，才可以只使用 `compose.yaml`。

## 1. 上线前确定方案

推荐的最小生产拓扑：

```text
浏览器 -> 云安全组 -> Nginx(80/443) -> 后端容器 -> MySQL 容器
```

先确定这些参数：

| 参数 | 示例 | 说明 |
| --- | --- | --- |
| `SERVER_IP` | `<server-public-ip>` | 云服务器公网 IP |
| `SSH_USER` | `root` | 建议后续改为普通部署用户 |
| `PROJECT_DIR` | `/opt/<project-name>` | 服务器项目目录 |
| `APP_DOMAIN` | `app.example.com` | 对外提供服务的规范域名 |
| `REDIRECT_DOMAINS` | `example.com,www.example.com` | 需要跳转的旧入口 |
| `DATABASE_NAME` | `<database-name>` | 生产数据库名称 |

如果服务器位于中国大陆，需要先确认备案要求。选择中国香港等非中国大陆地域通常不走中国大陆 ICP 备案流程，但仍应遵守云平台、域名注册商和当地适用法规。

## 2. 配置 SSH 免密登录

本机确认密钥：

```powershell
Get-ChildItem $env:USERPROFILE\.ssh
```

没有 ED25519 密钥时生成：

```powershell
ssh-keygen -t ed25519 -C "deployment-key"
```

首次使用服务器密码登录，将公钥追加到服务器：

```powershell
Get-Content $env:USERPROFILE\.ssh\id_ed25519.pub | ssh root@<SERVER_IP> "umask 077; mkdir -p ~/.ssh; cat >> ~/.ssh/authorized_keys"
```

验证不再需要密码：

```powershell
ssh -o BatchMode=yes -o PasswordAuthentication=no root@<SERVER_IP> "id -un && hostname"
```

不要把服务器密码写入文档、命令历史或 Git。免密验证成功后，再考虑关闭 SSH 密码登录。

## 3. 配置安全组和 DNS

安全组只开放必要端口：

| 端口 | 协议 | 来源 | 用途 |
| --- | --- | --- | --- |
| `22` | TCP | 个人固定 IP 更佳 | SSH 运维 |
| `80` | TCP | `0.0.0.0/0` | HTTP 跳转和 ACME 验证 |
| `443` | TCP | `0.0.0.0/0` | HTTPS 网站 |

不要开放公网 `3306`、`8000` 或前端开发端口 `5173`。Navicat 使用 SSH 隧道连接数据库。

DNS 控制台添加 A 记录：

| 主机记录 | 类型 | 记录值 |
| --- | --- | --- |
| `@` | `A` | `<SERVER_IP>` |
| 项目子域名 | `A` | `<SERVER_IP>` |
| `www` | `A` | `<SERVER_IP>`，如需兼容旧地址 |

DNS 只负责解析地址，主域名到子域名的 URL 跳转由 Nginx 返回 `301`。从本机和公共 DNS 检查：

```powershell
Resolve-DnsName app.example.com -Type A
Resolve-DnsName app.example.com -Server 8.8.8.8 -Type A
```

服务器内部还要检查防火墙和监听状态：

```bash
ss -ltn
ufw status
```

## 4. 安装 Docker 并同步代码

安装 Docker Engine 和 Compose Plugin，确认：

```bash
docker --version
docker compose version
```

推荐先在本地提交并推送代码，再在服务器拉取固定提交：

```bash
mkdir -p /opt/<project-name>
cd /opt/<project-name>
git clone <repository-url> .
git checkout <commit-or-tag>
```

若临时部署未提交的本地修改，使用归档或安全复制同步；部署完成后应尽快提交，使服务器版本可追踪、可回滚。

## 5. 创建生产配置

在服务器项目目录执行：

```bash
cp .env.production.example .env.production
chmod 600 .env.production
```

至少确认以下配置：

```env
APP_ENV=production
DATABASE_URL=mysql+asyncmy://root:<root-password>@mysql:3306/<database-name>
DATABASE_ECHO=false
CORS_ORIGINS=["https://shuhang.shaoji.site"]
MYSQL_ROOT_PASSWORD=<root-password>
MYSQL_DATABASE=<database-name>
OPENAI_API_KEY=<real-api-key>
JWT_SECRET_KEY=<random-secret-at-least-32-bytes>
```

关键规则：

- `DATABASE_URL` 的主机名使用 Compose 服务名 `mysql`，不要写服务器的 `127.0.0.1`。
- `MYSQL_ROOT_PASSWORD` 是 MySQL 首次初始化所需的密码，必须与实际数据库 root 密码一致。
- 已初始化的 MySQL 不会因修改 `.env.production` 自动改密码；改密码时要同时执行 `ALTER USER`，再重建依赖容器。
- `CORS_ORIGINS` 必须是合法 JSON 数组，只允许实际 HTTPS 来源，不要写 `*`。
- `.env.production`、证书私钥和数据库备份不能提交 Git、复制进前端镜像或写入 `VITE_*` 变量。

校验 Compose 文件但不要打印密钥：

```bash
docker compose -f compose.yaml -f compose.resume-agent.yaml config --quiet
```

## 6. 初始化 MySQL 和启动服务

`app/db/sales_agent.sql` 只适用于新数据库卷。MySQL 官方镜像只会在数据目录为空的首次初始化时执行它；如果脚本包含重建表语句，不要在已有生产数据上重新挂载执行。

首次启动仅启动 MySQL 和后端：

```bash
docker compose -f compose.yaml -f compose.resume-agent.yaml up --build --detach --wait mysql backend
docker compose -f compose.yaml -f compose.resume-agent.yaml ps
```

不要在证书签发前启动 `frontend`。当前仓库的 Nginx 要求证书已经存在，证书签发和前端启动顺序见下一节。

后续只检查数据库和后端时：

```bash
docker compose -f compose.yaml -f compose.resume-agent.yaml up --build --detach --wait mysql backend
```

查看失败原因：

```bash
docker compose -f compose.yaml -f compose.resume-agent.yaml logs --no-color --tail=200 mysql backend frontend
```

期望 MySQL 和后端为 `healthy`，前端发布宿主机 `80` 和 `443`。不要执行下面的命令，除非明确要删除数据库卷：

```bash
docker compose -f compose.yaml -f compose.resume-agent.yaml down -v
```

## 7. 配置 HTTPS

证书签发前必须满足：域名已经解析到服务器、云安全组放行 `80`、没有其它进程占用 `80`，并已创建证书目录。

如果前端容器已占用 `80`，先停止它：

```bash
docker compose -f compose.yaml -f compose.resume-agent.yaml stop frontend
mkdir -p certbot/conf certbot/www
```

本仓库当前固定使用 `shuhang.shaoji.site` 作为规范域名，且 `docker/tls.conf` 读取 `www.shaoji.site` 证书。使用 Certbot 独立模式首次签发时，实际部署应使用：

共享服务器上 Resume Agent 也使用这份证书，因此不能只申请 Sales Agent 的三个域名；必须保留 `resume.shaoji.site` SAN。首次签发或替换共享证书前，先确认 Resume Agent 的 DNS 已指向同一服务器，并与另一个项目协同验收。

```bash
docker run --rm --name sales-agent-certbot -p 80:80 \
  -v "$PWD/certbot/conf:/etc/letsencrypt" \
  -v "$PWD/certbot/www:/var/www/certbot" \
  certbot/certbot certonly --standalone --non-interactive \
  --agree-tos --no-eff-email --email <certificate-email> \
  --cert-name www.shaoji.site \
  -d shaoji.site -d www.shaoji.site -d shuhang.shaoji.site -d resume.shaoji.site
```

证书私钥在 `certbot/conf/live/` 下，必须只保存在服务器。若改用其它域名，必须同步修改 Nginx、证书名、所有 `-d` 参数和 `CORS_ORIGINS`，不能只替换其中一个占位符。

Nginx 应同时配置：

- HTTP 的 `/.well-known/acme-challenge/` 验证路径。
- HTTP 其它请求 `301` 到规范 HTTPS 域名。
- 规范域名的 `listen 443 ssl`、证书路径和 API 反向代理。
- 根域名、`www` 等跳转域名的 HTTPS `server` 块。证书必须覆盖这些域名，否则跳转前会出现证书不匹配。

启动并检查 Nginx：

```bash
docker compose -f compose.yaml -f compose.resume-agent.yaml up --build --detach frontend
docker compose -f compose.yaml -f compose.resume-agent.yaml exec -T frontend nginx -t
```

### 证书自动续期

Nginx 已运行后，把续期方式改为 Webroot，并执行模拟续期：

```bash
docker run --rm \
  -v "$PWD/certbot/conf:/etc/letsencrypt" \
  -v "$PWD/certbot/www:/var/www/certbot" \
  certbot/certbot reconfigure --cert-name www.shaoji.site \
  --webroot -w /var/www/certbot --non-interactive
```

可使用 `/etc/cron.d/<project-name>-certbot` 每日检查：

```cron
SHELL=/bin/sh
PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
17 3 * * * root docker run --rm -v /opt/<project-name>/certbot/conf:/etc/letsencrypt -v /opt/<project-name>/certbot/www:/var/www/certbot certbot/certbot renew --quiet && docker compose -f /opt/<project-name>/compose.yaml -f /opt/<project-name>/compose.resume-agent.yaml exec -T frontend nginx -s reload
```

```bash
chmod 644 /etc/cron.d/<project-name>-certbot
systemctl enable --now cron
systemctl is-active cron
```

## 8. 备份和 Navicat

备份前创建受限目录，备份文件不要放入 Git：

```bash
mkdir -p /opt/<project-name>/backups
chmod 700 /opt/<project-name>/backups
docker compose -f compose.yaml -f compose.resume-agent.yaml exec -T mysql sh -ec \
  'MYSQL_PWD="$MYSQL_ROOT_PASSWORD" mysqldump -u root -h 127.0.0.1 <database-name>' \
  > /opt/<project-name>/backups/<database-name>-$(date +%Y%m%d-%H%M%S).sql
chmod 600 /opt/<project-name>/backups/*.sql
```

Navicat 使用 SSH 通道：MySQL 主机填 `127.0.0.1`、端口填服务器回环端口、用户名和密码填数据库账号；SSH 主机填服务器 IP、端口 `22`、用户填 `<SSH_USER>`，验证方式选择本机 `id_ed25519` 私钥。

## 9. 公网验收

服务器本机：

```bash
docker compose -f compose.yaml -f compose.resume-agent.yaml config --quiet
docker compose -f compose.yaml -f compose.resume-agent.yaml ps
curl -fsS https://shuhang.shaoji.site/health
ss -ltn
```

本机公网网络：

```powershell
curl.exe -I http://shuhang.shaoji.site
curl.exe -I https://shuhang.shaoji.site
curl.exe https://shuhang.shaoji.site/health
```

验收标准：

- HTTP 返回 `301`，`Location` 指向规范 HTTPS 域名。
- 规范 HTTPS 首页和 `/health` 返回 `200`。
- 浏览器证书无域名不匹配、过期或不受信任提示。
- MySQL 和后端为 `healthy`，`3306` 和 `8000` 没有公网监听。
- 登录、会话创建、会话删除、同步聊天和流式聊天均至少人工验证一次。

## 10. 常见故障、更新和回滚

| 现象 | 优先检查 |
| --- | --- |
| 公网 80/443 超时 | 安全组、服务器防火墙、`ss -ltn`、DNS |
| Certbot 验证失败 | DNS、80 端口、是否停止占用 80 的前端 |
| HTTPS 返回 502 | 后端健康状态、后端日志、Nginx 上游服务名 |
| 后端持续重启 | `.env.production`、`APP_ENV`、数据库连接、CORS JSON 格式 |
| MySQL 提示未设置 root 密码 | 是否存在 `MYSQL_ROOT_PASSWORD` |
| HTTPS 失败 | 443 安全组、证书挂载、`nginx -t` |

更新前记录当前版本和状态：

```bash
git rev-parse HEAD
docker compose -f compose.yaml -f compose.resume-agent.yaml ps
```

更新或回滚到固定提交：

```bash
git checkout <commit-or-tag>
docker compose -f compose.yaml -f compose.resume-agent.yaml config --quiet
# 仅在 /etc/letsencrypt/live/www.shaoji.site 已存在且证书仍有效时执行
docker compose -f compose.yaml -f compose.resume-agent.yaml up --build --detach --wait
```

数据库迁移和应用发布分开验证；生产数据迁移前先备份，并保留回滚方案。
