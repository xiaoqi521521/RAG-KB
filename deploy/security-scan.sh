#!/usr/bin/env bash
set -euo pipefail

# 扫描源码、锁文件和 Compose 配置，覆盖 Python/Node 依赖、密钥和部署错误。
trivy fs --scanners vuln,secret,misconfig --exit-code 1 --ignore-unfixed .

# 构建后扫描实际发布镜像；BACKEND_IMAGE/FRONTEND_IMAGE 可由发布环境注入版本标签。
docker compose build --pull backend frontend
while IFS= read -r image; do
  [ -n "$image" ] || continue
  trivy image --scanners vuln,secret --exit-code 1 --ignore-unfixed "$image"
done < <(docker compose config --images | sort -u)
