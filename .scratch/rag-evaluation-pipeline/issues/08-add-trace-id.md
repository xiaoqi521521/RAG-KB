# 08 — 增加请求 Trace ID

**What to build:** 为评估、反馈和现有 HTTP 请求提供统一的 Trace ID，使调用方可以关联响应和安全日志，同时避免不可信请求头污染日志或形成高基数指标。

**Blocked by:** None — can start immediately

**Status:** resolved

- [x] 中间件读取 `X-Trace-Id`，安全值可以沿用，缺失或非法值替换为新 UUID。
- [x] 安全校验限制长度和字符集合，避免任意换行或日志注入内容进入上下文。
- [x] Trace ID 写入请求级日志上下文，并在响应头中回写同一值。
- [x] 请求完成或异常结束后始终清理上下文。
- [x] 并发请求的 Trace ID 相互隔离，不发生跨请求串号。
- [x] 全局日志格式可以读取 Trace ID，同时不要求业务日志重复传参。
- [x] Trace ID 不进入 Prometheus 或 OpenTelemetry 指标标签。
- [x] 现有错误响应也携带 Trace ID，且异常处理不泄露内部细节。
- [x] 测试覆盖合法沿用、缺失生成、非法替换、响应传播、异常清理和并发隔离。

## Comments

- 2026-07-15：统一接入请求 Trace ID，上下文通过 `ContextVar` 隔离，日志记录工厂自动注入低风险值；合法客户端值限定为 1 至 64 位 ASCII 字母、数字、点、下划线或连字符。
- 关闭 FastAPI 调试错误页，`APP_DEBUG` 仅控制 Uvicorn 热重载，确保未处理异常始终返回统一错误信封和 `X-Trace-Id`，不暴露 traceback。
- 验证：`uv run pytest -q`（290 passed）；`uv run ruff check .`（通过）；Trace ID 模块专项 Mypy 通过。`uv run mypy app` 仍报告 13 个既有问题。
