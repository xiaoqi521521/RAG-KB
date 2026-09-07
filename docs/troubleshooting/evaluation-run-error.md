# 评估运行错误排查指南

## 问题现象

点击"运行评估"按钮后没有任何反应，或出现错误提示。

## 快速诊断

### 1. 使用调试脚本检查状态

```bash
python scripts/debug_evaluation.py <知识库ID>
```

该脚本会检查：
- 知识库是否存在
- 评估数据集的数量和状态分布
- 是否有 ACTIVE 状态的问题
- 历史评估版本信息

### 2. 查看浏览器控制台

1. 打开浏览器开发者工具（F12）
2. 切换到 Console 标签
3. 点击"运行评估"按钮
4. 查看是否有错误信息输出

### 3. 查看后端日志

```bash
# Docker 环境
docker-compose logs -f backend --tail=100

# 本地开发环境
# 查看终端输出
```

## 常见问题和解决方案

### 1. 没有 ACTIVE 状态的评估数据集

**错误信息**：
- `没有可运行的 ACTIVE 标准问题（当前有 X 个非 ACTIVE 状态的问题），请将至少一个问题设置为"有效"状态`
- 前端提示：`没有可运行的 ACTIVE 标准问题，请先添加标准问题并设置为"有效"状态`

**原因**：评估服务只会运行状态为 ACTIVE 的标准问题。如果所有问题都是 CANDIDATE、NEEDS_REVIEW 或 ARCHIVED 状态，则无法运行评估。

**解决方案 A - 通过界面手动修改**：
1. 在"标准问题集"表格中找到要激活的问题
2. 点击该问题行的"编辑"按钮（铅笔图标）
3. 在弹出的对话框中，将"状态"下拉框改为"有效"(ACTIVE)
4. 点击"保存"
5. 重试运行评估

**解决方案 B - 使用脚本批量激活**：
```bash
# 试运行，查看会激活哪些问题
python scripts/activate_eval_datasets.py <知识库ID>

# 确认无误后实际执行
python scripts/activate_eval_datasets.py <知识库ID> --execute
```

### 2. 知识库中没有任何评估数据集

**错误信息**：`知识库中没有任何评估数据集，请先添加标准问题`

**原因**：该知识库还没有创建任何评估标准问题。

**解决方案**：
1. 点击"添加问题"按钮
2. 填写以下信息：
   - **问题**（必填）：用户会提问的标准问题
   - **期望答案**（可选）：用于评估生成答案的质量
   - **目标 chunk**（可选）：应该被检索命中的文档片段
   - **状态**：选择"有效"(ACTIVE)
3. 点击"保存"
4. 重试运行评估

**建议**：
- 至少添加 5-10 个有代表性的标准问题
- 问题应覆盖知识库的主要内容领域
- 如果标注了"期望答案"，系统会计算 RAGAS 指标（忠实度、答案相关性等）
- 如果标注了"目标 chunk"，系统会计算检索命中率和排序质量

### 3. RAG V4 管道未启用

**错误信息**：`正式评估要求启用 V4 RAG 管道`

**原因**：评估服务依赖 V4 RAG 管道，但当前配置使用的是其他版本。

**解决方案**：
1. 检查 `.env` 文件或环境变量
2. 确保 `RAG_QUERY_PIPELINE=v4`
3. 重启后端服务
4. 重试运行评估

### 4. 评估版本已存在

**错误信息**：`评估版本已存在`

**原因**：尝试使用已经存在的评估版本号运行评估（通常不应该发生，因为版本号是自动递增的）。

**可能的场景**：
- 并发运行评估（两个用户同时点击）
- 数据库同步问题

**解决方案**：
1. 刷新页面，查看最新的评估版本号
2. 等待几秒后重试
3. 如果问题持续，联系管理员检查数据库

### 5. 权限不足

**错误信息**：`权限不足` 或 403 错误

**原因**：当前用户对该知识库没有 ADMIN 权限。

**解决方案**：
1. 确认你的账号是该知识库的管理员
2. 如果不是，联系知识库创建者或系统管理员授予权限
3. 如果你是系统管理员，可以直接在数据库中修改权限

### 6. API 超时

**错误信息**：`timeout` 或 `Request timeout`

**原因**：评估运行时间较长，超过了默认超时时间（60秒）。

**可能的原因**：
- 评估问题数量太多
- RAGAS 评估器响应缓慢
- LLM API 调用超时

**解决方案**：
1. 减少 ACTIVE 状态的问题数量（分批评估）
2. 检查 LLM API 服务状态
3. 增加超时配置：
   ```env
   RAGAS_TIMEOUT_SECONDS=120  # 默认 60
   ```

### 7. 依赖服务不可用

**错误信息**：`503 Service Unavailable` 或 `意图识别服务暂不可用`

**原因**：评估依赖的外部服务（LLM、Embedding、Reranker）不可用。

**解决方案**：
1. 检查 API 密钥配置：
   ```env
   DASHSCOPE_API_KEY=your-key
   OPENAI_API_KEY=your-key
   ```
2. 检查 API 端点是否可访问
3. 查看是否有网络代理问题
4. 检查 API 配额是否用完

## 诊断清单

运行评估前，请确认以下各项：

- [ ] 知识库已选择且有 ADMIN 权限
- [ ] 至少有 1 个状态为 ACTIVE 的评估数据集
- [ ] RAG_QUERY_PIPELINE=v4
- [ ] LLM API 密钥已配置且有效
- [ ] 数据库连接正常
- [ ] Redis 连接正常（用于缓存）
- [ ] 没有其他评估正在运行

## 调试工具

### 1. 检查评估数据集状态
```bash
python scripts/debug_evaluation.py <知识库ID>
```

### 2. 批量激活评估数据集
```bash
python scripts/activate_eval_datasets.py <知识库ID> --execute
```

### 3. 查看实时日志
```bash
# Docker 环境
docker-compose logs -f backend

# 查看评估相关日志
docker-compose logs backend | grep -i evaluation

# 查看错误日志
docker-compose logs backend | grep -i error
```

### 4. 数据库直接查询
```sql
-- 查看评估数据集状态分布
SELECT status, COUNT(*) as count
FROM eval_dataset
WHERE kb_id = <知识库ID>
GROUP BY status;

-- 查看最近的评估结果
SELECT eval_version, COUNT(*) as question_count, 
       AVG(CASE WHEN hit THEN 1.0 ELSE 0.0 END) as hit_rate
FROM eval_result er
JOIN eval_dataset ed ON er.dataset_id = ed.id
WHERE ed.kb_id = <知识库ID>
GROUP BY eval_version
ORDER BY eval_version DESC
LIMIT 5;
```

## 获取帮助

如果以上方法都无法解决问题，请：

1. 收集以下信息：
   - 浏览器控制台的完整错误信息
   - 后端日志（最近 100 行）
   - 调试脚本的输出
   - 评估数据集的状态分布

2. 提交 Issue 或联系技术支持
