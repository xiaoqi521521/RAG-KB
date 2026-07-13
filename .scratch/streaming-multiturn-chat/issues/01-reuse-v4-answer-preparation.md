# 01 — 提取可复用的 V4 回答准备流程

**What to build:** 在不改变既有同步知识库问答外部行为的前提下，形成可由同步和流式问答共同使用的 V4 回答准备边界，使检索、精排、上下文裁剪和引用准备继续保持同一套质量语义。

**Blocked by:** None — can start immediately.

**Status:** ready-for-agent

- [x] 同步 RAG 问答保持既有回答、拒答、引用与错误行为。
- [x] 流式问答可以复用同一套 V4 参考内容准备结果，不复制检索和质量保障逻辑。
- [x] 现有同步问答测试保持通过，并覆盖共享边界的外部可观察行为。

## Comments

- 已提取 V4 参考内容准备和消息构建边界；`uv run pytest tests/api/test_rag.py -q` 与对应 Ruff 检查通过。
