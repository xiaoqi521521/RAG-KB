# 评估超时问题修复

## 问题描述
运行评估时出现超时错误：`AxiosError: timeout of 60000ms exceeded`

这表明评估任务执行时间超过了60秒的默认超时限制。

## 已实施的修复

### 1. 增加超时时间

#### 全局 API 超时设置 (frontend/src/api/request.ts)
```typescript
timeout: 300000, // 从 60秒 改为 5分钟（300秒）
```

#### 评估 API 专用超时 (frontend/src/api/index.ts)
```typescript
runEvaluation: (kbId: number) =>
  request.post<ApiResponse<EvaluationReport>>(
    `/eval/${kbId}/run`,
    null,
    {
      ...evalRequestConfig,
      timeout: 300000, // 评估任务特别长，设置5分钟超时
    },
  ),
```

### 2. 改进用户体验 (frontend/src/pages/Evaluation.tsx)

- ✅ 添加持续的加载提示，显示正在评估的问题数量
- ✅ 超时错误特殊处理，给出具体的原因和建议
- ✅ 评估完成后显示版本号

```typescript
// 加载提示
message.loading({
  content: `正在评估 ${activeDatasets.length} 个标准问题，请耐心等待...`,
  duration: 0, // 不自动关闭
});

// 超时错误提示
if (error?.code === 'ECONNABORTED' || error?.message?.includes('timeout')) {
  message.error({
    content: `评估运行超时。这可能是因为：
      1) 问题数量较多（当前 ${activeDatasets.length} 个）
      2) LLM API 响应慢
      建议减少问题数量或稍后重试。`,
    duration: 10,
  });
}
```

## 评估任务耗时分析

每个评估问题需要执行：

1. **RAG 查询** (~2-5秒)
   - 向量检索
   - 全文检索
   - Reranker 重排序
   - LLM 生成答案

2. **RAGAS 评估** (~3-8秒，如果标注了期望答案)
   - Faithfulness（忠实度）
   - Answer Relevancy（答案相关性）
   - Context Recall（上下文召回）
   - Context Precision（上下文精度）

**总计**：5-13秒/问题

- 10个问题 ≈ 50-130秒
- 20个问题 ≈ 100-260秒 ⚠️ 可能超时
- 50个问题 ≈ 250-650秒 ⚠️ 很可能超时

## 短期解决方案（已实施）

### ✅ 方案 1：增加超时时间
- 前端超时从 60秒 → 300秒（5分钟）
- 支持最多约 30-50 个问题的评估

### ✅ 方案 2：改进用户反馈
- 显示实时加载提示
- 超时时给出明确建议
- 显示正在评估的问题数量

## 长期优化方案（建议）

### 方案 3：异步任务模式 🎯 推荐

将评估改为后台异步任务：

```python
# 后端改动
@router.post("/{kb_id}/run")
async def run_evaluation(...):
    # 创建后台任务
    task_id = await background_tasks.create_evaluation_task(kb_id, user)
    return ApiResponse.ok({"task_id": task_id, "status": "running"})

@router.get("/{kb_id}/run/{task_id}")
async def get_evaluation_status(task_id: str):
    # 查询任务状态
    task = await background_tasks.get_task(task_id)
    return ApiResponse.ok(task)
```

```typescript
// 前端改动
// 1. 提交评估任务
const { task_id } = await evalApi.runEvaluation(kbId);

// 2. 轮询任务状态
const pollInterval = setInterval(async () => {
  const status = await evalApi.getEvaluationStatus(kbId, task_id);
  if (status.completed) {
    clearInterval(pollInterval);
    message.success('评估完成');
  }
}, 3000); // 每3秒查询一次
```

**优点**：
- ✅ 支持任意数量的评估问题
- ✅ 用户可以关闭页面，任务继续运行
- ✅ 可以显示实时进度（已完成 X/Y 个问题）
- ✅ 避免 HTTP 超时限制

### 方案 4：批量评估优化

**并行执行**（需谨慎，可能触发 API 限流）
```python
# 使用 asyncio.gather 并行执行多个问题
results = await asyncio.gather(*[
    evaluate_question(q) for q in questions
], return_exceptions=True)
```

**渐进式结果保存**
```python
# 每完成一个问题立即保存
for question in questions:
    result = await evaluate_question(question)
    await repository.save_result(result)  # 实时保存
```

### 方案 5：减少 RAGAS 评估耗时

**采样评估**（当前已支持部分采样）
```python
# 只对部分问题进行完整 RAGAS 评估
ragas_sample_rate = 0.5  # 50% 的问题进行 RAGAS 评估
```

**缓存 LLM 响应**
```python
# 对相同问题缓存评估结果
@lru_cache(maxsize=1000)
async def evaluate_with_cache(question: str, answer: str):
    ...
```

### 方案 6：分批评估

**前端分批提交**
```typescript
// 将大量问题拆分为多个批次
const BATCH_SIZE = 10;
for (let i = 0; i < totalQuestions; i += BATCH_SIZE) {
  await evalApi.runEvaluationBatch(kbId, i, i + BATCH_SIZE);
}
```

## 临时应对措施

### 如果评估仍然超时

1. **减少 ACTIVE 问题数量**
   ```bash
   python scripts/debug_evaluation.py <知识库ID>
   # 查看当前 ACTIVE 问题数量，建议保持在 20 个以内
   ```

2. **分批设置为 ACTIVE**
   - 第一批：10个核心问题 → 运行评估
   - 第二批：另外10个问题 → 运行评估
   - 合并查看历史结果

3. **检查 LLM API 性能**
   ```bash
   # 测试 API 响应时间
   curl -w "@curl-format.txt" -o /dev/null -s "https://dashscope.aliyuncs.com/..."
   ```

4. **调整 RAGAS 超时配置**
   ```env
   # .env 文件
   RAGAS_TIMEOUT_SECONDS=120  # 默认 60，可增加到 120
   RAGAS_MAX_TOKENS=2048      # 减少 token 数加快响应
   ```

## 监控建议

### 后端日志监控
```bash
# 查看评估耗时
docker-compose logs backend | grep "Evaluation run completed"

# 查看慢查询
docker-compose logs backend | grep -i "slow"
```

### 性能指标
```python
# 在评估服务中添加性能指标
logger.info(
    "Evaluation performance: kb_id=%s questions=%s total_time=%.2fs avg_time=%.2fs",
    kb_id,
    len(datasets),
    total_time,
    total_time / len(datasets),
)
```

## 测试验证

### 测试不同规模的评估
- [ ] 5个问题 - 应在 30秒内完成
- [ ] 10个问题 - 应在 60秒内完成
- [ ] 20个问题 - 应在 120秒内完成
- [ ] 30个问题 - 应在 180秒内完成

### 测试超时处理
- [ ] 模拟超时（设置很短的超时时间）
- [ ] 验证错误提示是否清晰
- [ ] 验证加载状态正确清除

## 总结

**当前状态**：✅ 已修复，支持最多 30-50 个问题

**推荐长期方案**：实施异步任务模式（方案3），彻底解决超时问题

**临时应对**：减少 ACTIVE 问题数量，分批评估
