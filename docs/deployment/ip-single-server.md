# 新服务器仅使用 IP 访问，不再保留旧的域名/HTTPS 入口。
# 历史域名共享部署说明见 cloud-server-runbook.md 与 shared-deployment-boundaries.md。

当前生产地址：

- Portal：http://123.206.117.213/
- Resume Agent：http://123.206.117.213:8080/
- RAG-KB：http://123.206.117.213:8081/

部署目录固定为 /opt/shaoji-ip。三套服务互不共享 Nginx：

- compose.portal-ip.yaml：Portal，占 80
- compose.resume-ip.yaml：Resume Agent，占 8080
- compose.rag-kb-ip.yaml：RAG-KB，占 8081

云安全组必须放通 TCP 入站：22、80、8080、8081。
不要放通 5432、6379、9000、8000、3000、9090。

仓库中的 `deploy/ip/` 保存三套 Compose 与 Nginx 的版本化副本。
服务器实际命令与备份恢复步骤见 `deploy/ip/README.md`。
