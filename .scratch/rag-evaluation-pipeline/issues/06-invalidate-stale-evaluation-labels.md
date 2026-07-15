# 06 — 文档发布时使旧标注失效

**What to build:** 当文档新版本成功发布时，自动找出引用该文档旧 chunk 的活动标准问题并标记为待审核，使后续正式评估不会继续使用已经删除的期望证据。

**Blocked by:** 03 — 管理标准问题集并标注当前 Chunk

**Status:** resolved

- [x] 新版本 chunk 全部成功写入后、旧 chunk 删除前读取当前旧版本的 chunk ID。
- [x] 只把期望 chunk 数组与旧 ID 有重叠的 `ACTIVE` 问题更新为 `NEEDS_REVIEW`。
- [x] 更新同时按知识库范围和活动状态过滤，不影响其他知识库或其他生命周期状态。
- [x] 待审核问题保存稳定的文档重建原因。
- [x] 标注失效、文档版本发布和旧 chunk 删除在同一数据库事务内完成。
- [x] 重建任务提交、处理中或最终失败时，当前标准问题状态保持不变。
- [x] 首次索引没有旧 chunk 时不产生无意义状态更新。
- [x] 待审核问题不再被正式评估加载。
- [x] 测试覆盖多个文档、数组重叠与不重叠、其他状态、成功发布、失败重建和事务回滚。

## Comments

- 2026-07-15：索引发布在新 chunk 写入后读取旧版本 ID，发布文档版本后按 `kb_id + ACTIVE + expected_chunk_ids overlap` 批量标记 `NEEDS_REVIEW`，固定原因为 `document_reindexed`，随后删除旧 chunk。
- 后台索引失败时先回滚发布事务，再单独记录失败和重试状态；文档版本、标注失效与旧 chunk 删除不会部分提交。首次索引不读取旧 ID，也不触发标注更新。
- 验证：PostgreSQL 临时表集成测试真实覆盖多文档重叠、不重叠、其他状态、跨知识库和事务回滚且未跳过；`uv run pytest -q`（314 passed）；`uv run ruff check .`（通过）；Ticket 06 核心模块专项 Mypy 通过。`uv run mypy app` 仍报告 13 个既有问题。
