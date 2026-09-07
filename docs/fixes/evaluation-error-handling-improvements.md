# 评估错误处理改进

## 修复日期
2026-09-07

## 问题描述
运行评估功能时，如果遇到错误（如没有 ACTIVE 状态的数据集），用户可能无法获得清晰的错误提示或诊断信息。

## 改进内容

### 1. 前端改进

#### 增强错误显示 (`frontend/src/pages/Evaluation.tsx`)
- ✅ 添加 `console.error` 输出，便于在浏览器控制台查看完整错误
- ✅ 增加错误消息显示时长（8秒），确保用户能看到
- ✅ 改进错误提示文案，引导用户查看控制台

#### 前置验证检查 (`frontend/src/pages/Evaluation.tsx`)
- ✅ 在发送请求前检查是否有 ACTIVE 状态的数据集
- ✅ 如果没有，显示友好的警告消息，避免不必要的 API 请求
- ✅ 提示用户具体的解决方案

### 2. 后端改进

#### 更详细的错误消息 (`app/evaluation/service.py`)
- ✅ 区分"完全没有数据集"和"有数据集但都不是 ACTIVE"两种情况
- ✅ 在错误消息中包含非 ACTIVE 数据集的数量
- ✅ 提供明确的解决方案指引

#### 增强日志记录 (`app/evaluation/service.py`, `app/api/routes/evaluation.py`)
- ✅ 记录评估运行的开始和结束
- ✅ 记录评估版本号
- ✅ 记录数据集数量
- ✅ 记录用户 ID，便于问题追踪
- ✅ 在没有 ACTIVE 数据集时记录警告日志

### 3. 诊断工具

#### 调试脚本 (`scripts/debug_evaluation.py`)
新增命令行工具，用于快速诊断评估相关问题：
```bash
python scripts/debug_evaluation.py <知识库ID>
```

功能：
- 检查知识库是否存在
- 统计评估数据集的状态分布
- 显示前 5 个问题的状态和内容
- 显示历史评估版本信息
- 给出明确的问题诊断和解决建议

#### 批量激活脚本 (`scripts/activate_eval_datasets.py`)
新增命令行工具，用于批量将数据集设置为 ACTIVE：
```bash
# 试运行
python scripts/activate_eval_datasets.py <知识库ID>

# 实际执行
python scripts/activate_eval_datasets.py <知识库ID> --execute
```

功能：
- 查找所有非 ACTIVE、非 ARCHIVED 的数据集
- 支持试运行模式（默认）
- 批量更新为 ACTIVE 状态

### 4. 文档

#### 故障排查指南 (`docs/troubleshooting/evaluation-run-error.md`)
新增完整的故障排查文档，包括：
- 问题现象描述
- 快速诊断步骤
- 7 种常见问题及解决方案
- 诊断清单
- 调试工具使用说明
- 数据库查询示例
- 获取帮助的流程

## 使用示例

### 场景 1：运行评估时提示没有 ACTIVE 问题

**步骤 1：诊断**
```bash
python scripts/debug_evaluation.py 1
```

输出示例：
```
=== 评估功能调试 ===
检查知识库 ID: 1

✅ 知识库: HR知识库 (ID: 1)
   创建者: 1
   是否删除: False

📊 评估数据集总数: 5
   ⚠️ CANDIDATE: 3 个
   ⚠️ NEEDS_REVIEW: 2 个

❌ 没有 ACTIVE 状态的评估数据集
   解决方案: 编辑至少一个问题，将状态设置为'有效'(ACTIVE)

   现有问题列表:
      1. [CANDIDATE] 员工入职需要准备什么材料？
      2. [CANDIDATE] 年假怎么申请？
      3. [NEEDS_REVIEW] 薪资发放时间是什么时候？
      4. [CANDIDATE] 如何报销差旅费用？
      5. [NEEDS_REVIEW] 五险一金缴纳比例是多少？

📈 尚未运行过评估，下一个版本将是: 1
```

**步骤 2：修复**

方法 A - 使用脚本批量激活：
```bash
python scripts/activate_eval_datasets.py 1 --execute
```

方法 B - 通过界面手动修改：
1. 在评估页面点击问题的"编辑"按钮
2. 将状态改为"有效"
3. 保存

**步骤 3：重试**
点击"运行评估"按钮

### 场景 2：评估运行失败，不确定原因

**步骤 1：查看浏览器控制台**
1. 按 F12 打开开发者工具
2. 切换到 Console 标签
3. 查找以 "Evaluation run failed:" 开头的错误

**步骤 2：查看后端日志**
```bash
docker-compose logs backend | grep -i evaluation | tail -50
```

**步骤 3：运行诊断脚本**
```bash
python scripts/debug_evaluation.py <知识库ID>
```

## 技术细节

### 错误消息映射

| 后端错误 | 前端显示 | HTTP 状态码 |
|---------|---------|-----------|
| 没有任何评估数据集 | "知识库中没有任何评估数据集，请先添加标准问题" | 409 |
| 没有 ACTIVE 数据集 | "没有可运行的 ACTIVE 标准问题（当前有 X 个非 ACTIVE 状态的问题），请将至少一个问题设置为"有效"状态" | 409 |
| 评估版本已存在 | "评估版本已存在" | 409 |
| RAG V4 未启用 | "正式评估要求启用 V4 RAG 管道" | 503 |
| 权限不足 | "权限不足" | 403 |

### 日志格式

评估运行相关的日志采用结构化格式，便于过滤和分析：

```python
# 开始运行
logger.info("Evaluation run starting: kb_id=%s user_id=%s", kb_id, user.id)

# 版本确定
logger.info("Evaluation version determined: kb_id=%s eval_version=%s", kb_id, eval_version)

# 版本冲突
logger.warning("Evaluation version conflict: kb_id=%s eval_version=%s", kb_id, eval_version)

# 没有 ACTIVE 数据集
logger.warning("No ACTIVE datasets found: kb_id=%s total_datasets=%s", kb_id, len(all_datasets))

# 开始执行
logger.info("Starting evaluation execution: kb_id=%s eval_version=%s dataset_count=%s", kb_id, eval_version, len(datasets))

# 完成
logger.info("Evaluation run completed: kb_id=%s eval_version=%s", kb_id, report.eval_version)
```

## 测试建议

### 手动测试场景

1. **无数据集场景**
   - 创建一个新知识库
   - 不添加任何评估问题
   - 点击"运行评估"
   - 预期：显示"知识库中没有任何评估数据集"

2. **无 ACTIVE 数据集场景**
   - 添加几个评估问题，全部设为 CANDIDATE
   - 点击"运行评估"
   - 预期：前端提前拦截，显示友好提示

3. **正常场景**
   - 至少添加 1 个 ACTIVE 状态的问题
   - 点击"运行评估"
   - 预期：成功运行，显示评估结果

4. **日志验证**
   - 在各种场景下运行评估
   - 检查后端日志是否包含结构化日志信息
   - 确认日志包含 kb_id、user_id、eval_version 等关键信息

## 未来改进方向

1. **实时进度显示**
   - 显示当前评估进度（已完成 X/总共 Y 个问题）
   - 使用 WebSocket 或 SSE 推送进度更新

2. **异步评估**
   - 将评估任务改为后台异步执行
   - 避免长时间阻塞前端请求

3. **评估队列**
   - 支持排队机制，避免并发冲突
   - 显示队列位置和预计等待时间

4. **更详细的错误分类**
   - 区分临时错误（可重试）和永久错误（需修复配置）
   - 提供自动重试机制

5. **评估预检**
   - 在点击按钮前自动检查所有前置条件
   - 显示检查结果和阻塞项

## 相关文件

### 修改的文件
- `frontend/src/pages/Evaluation.tsx` - 前端评估页面
- `app/evaluation/service.py` - 评估服务逻辑
- `app/api/routes/evaluation.py` - 评估 API 路由

### 新增的文件
- `scripts/debug_evaluation.py` - 诊断脚本
- `scripts/activate_eval_datasets.py` - 批量激活脚本
- `docs/troubleshooting/evaluation-run-error.md` - 故障排查文档
- `docs/fixes/evaluation-error-handling-improvements.md` - 本文档

## 回滚方案

如果这些改进导致问题，可以通过 Git 回滚：

```bash
git checkout HEAD~1 -- frontend/src/pages/Evaluation.tsx
git checkout HEAD~1 -- app/evaluation/service.py
git checkout HEAD~1 -- app/api/routes/evaluation.py
```

注意：调试脚本和文档可以保留，它们不影响运行时行为。
