FROM python:3.12.11-slim-bookworm

COPY --from=ghcr.io/astral-sh/uv:0.8.14 /uv /uvx /bin/

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_NO_PROGRESS=1 \
    SERVER_HOST=0.0.0.0 \
    SERVER_PORT=8000 \
    SERVER_LIMIT_CONCURRENCY=100 \
    SERVER_TIMEOUT_KEEP_ALIVE_SECONDS=10 \
    SERVER_TIMEOUT_GRACEFUL_SHUTDOWN_SECONDS=30

WORKDIR /app

COPY pyproject.toml uv.lock ./
RUN uv sync --locked --no-dev

COPY app ./app

EXPOSE 8000

CMD ["/app/.venv/bin/python", "-m", "app.main"]
