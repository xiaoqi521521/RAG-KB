# 03 — 接入 HyDE 与忠实性检测 Token 统计

**What to build:** 让 HyDE 查询改写和后台忠实性检测保留 provider usage，并分别进入 `hyde` 与 `faithfulness_check` 统计。两类内部调用的聊天输入归入 `input`，输出归入各自阶段；忠实性检测继续是非阻断后台观测。

**Blocked by:** 01 — 建立六类 Token 统计契约与最终回答路径

**Status:** resolved

- [ ] HyDE 模型响应在清洗文本的同时保留并记录 provider 输入/输出 usage。
- [ ] HyDE 输出写入 `hyde`，输入写入 `input`，缓存命中或生成失败不增加不存在的 Token。
- [ ] 忠实性检测输出写入 `faithfulness_check`，输入写入 `input`，并归属触发该在线请求的当前用户。
- [ ] 忠实性检测 usage 记录失败、超时或后台任务异常不改变最终回答结果。
- [ ] 抽样跳过不调用模型，也不记录 Token；provider usage 缺失只产生 unavailable 观测。
- [ ] 覆盖 HyDE 缓存/失败、忠实性抽样/超时/异常、输入输出提取、用户 Redis 和 Prometheus 指标测试。
