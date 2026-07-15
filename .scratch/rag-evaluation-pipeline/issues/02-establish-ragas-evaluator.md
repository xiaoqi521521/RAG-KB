# 02 — 建立可运行的 RAGAS 评估适配器

**What to build:** 提供一个可由正式评估调用的 RAGAS 适配器，复用现有回答模型与 Embedding 客户端计算 Faithfulness、Answer Relevancy、Context Recall 和 Context Precision，并隔离依赖兼容、并发、超时、重试和分数校验细节。

**Blocked by:** None — can start immediately

**Status:** resolved

- [x] 使用 `uv` 锁定能够在当前 Python 与 LangChain 环境真实导入和运行的 RAGAS 依赖组合。
- [x] 依赖元数据和锁文件同步更新，不修改虚拟环境内容或伪造兼容模块。
- [x] 存在针对生产实际使用 RAGAS 类型的运行时导入 smoke test。
- [x] 适配器通过单一公开接口接收问题、实际回答、期望答案和实际参考内容。
- [x] 四项指标使用各自正确的输入，并返回独立可空的 `0.0 ~ 1.0` 分数。
- [x] 四项适用指标在单个问题内并发执行，最大并发为四路。
- [x] 每项指标独立限制为 30 秒，并仅对超时、限流和暂时性外部错误最多重试一次。
- [x] 输入错误、解析错误、`NaN`、无穷值和越界分数不重试，并作为该指标失败返回。
- [x] 单项失败不抹除其他成功指标，错误输出不包含问题、回答、参考内容、Prompt 或供应商响应正文。
- [x] 测试通过适配器公开接口和外部模型/Embedding 测试替身验证行为，不 mock RAGAS 私有实现。

## Comments

- 2026-07-15：锁定 `ragas==0.4.3` 与 `langchain-community==0.3.31`，解决 RAGAS 导入已移除 VertexAI 模块的问题。适配器采用官方现代 `metrics.collections` 异步接口，复用现有 ChatOpenAI 底层异步客户端和 `text-embedding-v3` 客户端。
- 四项指标单题内并发执行，每次尝试默认超时 30 秒；OpenAI SDK 与 Instructor 内部各只允许一次尝试，外层仅对超时、限流和临时连接故障再尝试一次。错误结果只保存指标名与低基数分类，并记录不含业务正文的结果、重试和耗时观测。
- 验证：`uv run pytest -v`（265 passed）；`uv run ruff check .`（通过）；新评估模块专项 Mypy 通过。`uv run mypy app` 仍报告 13 个既有或并行改动问题，均不位于本 Ticket 修改文件。
