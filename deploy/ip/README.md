# IP-only single-server deployment

This directory contains the production-side Compose and Nginx files for the current server.
The public server is addressed by IP only, so it does not rely on an ICP-filed domain or TLS
certificates.

## Ports

| Port | Project | Public | Notes |
| --- | --- | --- | --- |
| `22` | SSH | no | restrict to the operator IP where possible |
| `80` | Portal | yes | `http://123.206.117.213/` |
| `8080` | Resume Agent | yes | `http://123.206.117.213:8080/` |
| `8081` | RAG-KB | yes | `http://123.206.117.213:8081/` |
| `3000`, `9090` | Grafana, Prometheus | no | bound to `127.0.0.1` |

Open `80`, `8080`, and `8081` in the cloud security group. Do not expose
`5432`, `6379`, `9000`, or the backend's `8000` port.

## Directory layout

The server uses `/opt/shaoji-ip` as the parent directory. The RAG-KB repository is checked out at
`/opt/shaoji-ip`, so the paths in `deploy/ip/rag-kb/compose.yaml` are relative to the repository
root.

```text
/opt/shaoji-ip/
  compose.portal-ip.yaml
  compose.resume-ip.yaml
  deploy/ip/...                 # Archived copy of the deployed configuration
  web/                          # Portal static files
  resume-agent/
  data/{postgres,minio,redis,prometheus,grafana,resume-metrics}/
  .env                          # RAG-KB runtime secrets, never commit
  .env.compose                  # Compose interpolation values, never commit
```

## Start

```bash
cd /opt/shaoji-ip

# Portal
docker compose -f compose.portal-ip.yaml up -d

# Resume Agent
docker compose -f compose.resume-ip.yaml up -d

# RAG-KB
docker compose --env-file .env.compose -f compose.rag-kb-ip.yaml up -d
```

Before starting RAG-KB, create `.env.compose` from `deploy/ip/rag-kb/.env.compose.example` and fill
in the four secret values. The RAG-KB `.env` must use Docker service names:

```env
DATABASE_URL=postgresql+asyncpg://ragkb:<password>@db:5432/ragkb
SYNC_DATABASE_URL=postgresql+psycopg://ragkb:<password>@db:5432/ragkb
REDIS_URL=redis://redis:6379/0
MINIO_ENDPOINT=minio:9000
```

## Smoke test

```bash
curl -fsS http://127.0.0.1/
curl -fsS http://127.0.0.1:8080/api/health
curl -fsS http://127.0.0.1:8081/api/v1/health/ready
```

Expected results: Portal returns HTML, Resume returns `{"status":"ready"}`, and RAG-KB returns
`{"status":"READY","checks":{"database":true,"redis":true,"minio":true}}`. The login endpoint
must not return `401` or `500`.

## Backup

Use `pg_dump` for PostgreSQL, archive the MinIO bind mount, and archive the Resume SQLite
directory. Keep backups outside the Docker volumes and never commit `.env` files.

```bash
mkdir -p /opt/shaoji-ip/backups
chmod 700 /opt/shaoji-ip/backups
docker exec shaoji-ip-db-1 pg_dump -U ragkb -d ragkb -Fc \
  > /opt/shaoji-ip/backups/ragkb-$(date +%Y%m%d-%H%M%S).dump
tar -C /opt/shaoji-ip/data -czf /opt/shaoji-ip/backups/minio-$(date +%Y%m%d-%H%M%S).tar.gz minio
tar -C /opt/shaoji-ip/data -czf /opt/shaoji-ip/backups/resume-metrics-$(date +%Y%m%d-%H%M%S).tar.gz resume-metrics
chmod 600 /opt/shaoji-ip/backups/*
```

## Restore notes

- Restore PostgreSQL with `pg_restore -U ragkb -d ragkb --clean --if-exists`.
- Remove the existing MinIO contents only after taking a fresh backup, then extract the archive
  into `/opt/shaoji-ip/data/minio`.
- The Resume container runs as UID `100` / GID `101`. After restoring its SQLite files, keep the
  directory owned by `100:101`; otherwise startup fails with `unable to open database file`.
- RAG-KB backend health is independent of the frontend. If the page returns 502, check the
  `rag-kb-backend` health and the Nginx `proxy_pass` service name.
