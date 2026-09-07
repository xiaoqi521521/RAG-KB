# 评估运行错误修复说明

## 问题
运行评估时报错，主要包括：
1. 缺少清晰的错误提示和排查工具
2. **超时错误**：`AxiosError: timeout of 60000ms exceeded`

## 已实施的修复

### 1. 前端改进
- ✅ 增强错误显示，添加控制台日志输出
- ✅ 添加前置检查：运行前验证是否有 ACTIVE 状态的数据集
- ✅ 改进错误提示文案和显示时长
- ✅ **增加超时时间**：从 60秒 → 300秒（5分钟）
- ✅ **超时错误特殊处理**：给出具体原因和建议
- ✅ **持续加载提示**：显示正在评估的问题数量

### 2. 后端改进  
- ✅ 更详细的错误消息（区分"无数据集"和"无 ACTIVE 数据集"）
- ✅ 在错误消息中包含数据集数量信息
- ✅ 增强日志记录（记录 kb_id、user_id、eval_version 等关键信息）

### 3. 新增诊断工具

#### 评估状态检查脚本
```bash
python scripts/debug_evaluation.py <知识库ID>
```
快速诊断评估相关问题，显示数据集状态分布。

#### 批量激活脚本
```bash
python scripts/activate_eval_datasets.py <知识库ID> --execute
```
批量将非 ACTIVE 数据集设置为 ACTIVE 状态。

### 4. 完整文档
- 📖 [故障排查指南](../troubleshooting/evaluation-run-error.md) - 7种常见问题及解决方案
- 📖 [超时问题修复](evaluation-timeout-fix.md) - 超时错误的详细说明和优化方案
- 📖 [详细修复说明](evaluation-error-handling-improvements.md) - 完整的技术细节

## 快速排查步骤

### 情况1：超时错误（timeout exceeded）

**现象**：浏览器控制台显示 `AxiosError: timeout of 60000ms exceeded`

**原因**：评估任务执行时间过长（每个问题需要 5-13秒）

**解决方案**：

1. **已修复**：超时时间增加到 5分钟，现在可以支持 30-50 个问题
   
2. **如果仍然超时**，减少 ACTIVE 问题数量：
   ```bash
   # 查看当前状态
   python scripts/debug_evaluation.py <知识库ID>
   
   # 如果问题太多，可以临时改为 CANDIDATE 状态
   # 通过界面编辑，只保留 10-20 个核心问题为 ACTIVE
   ```

3. **分批评估**：
   - 第一批：10个核心问题 → 运行评估
   - 第二批：另外10个问题 → 运行评估

**详细说明**：查看 [超时问题修复文档](evaluation-timeout-fix.md)

### 情况2：没有 ACTIVE 数据集

**现象**：提示 `没有可运行的 ACTIVE 标准问题`

**快速解决**：
```bash
# 检查状态
python scripts/debug_evaluation.py <知识库ID>

# 批量激活
python scripts/activate_eval_datasets.py <知识库ID> --execute
```

或在界面中：
1. 点击问题的"编辑"按钮
2. 将"状态"改为"有效"(ACTIVE)  
3. 保存

### 情况3：其他错误

1. **查看浏览器控制台**（F12 → Console）
2. **运行诊断脚本**：`python scripts/debug_evaluation.py <知识库ID>`
3. **查看后端日志**：`docker-compose logs backend | grep -i evaluation`
4. **参考文档**：[故障排查指南](../troubleshooting/evaluation-run-error.md)

## 性能参考

每个评估问题耗时：**5-13秒**

| 问题数量 | 预计耗时 | 状态 |
|---------|---------|------|
| 5个 | 25-65秒 | ✅ 正常 |
| 10个 | 50-130秒 | ✅ 正常 |
| 20个 | 100-260秒 | ⚠️ 接近上限 |
| 30个 | 150-390秒 | ⚠️ 可能超时 |
| 50个+ | 250秒+ | ❌ 很可能超时 |

**建议**：保持 ACTIVE 问题数量在 **20个以内**以获得最佳体验。

## 测试验证

修复后请测试以下场景：
- [ ] 无数据集时运行评估（应提示"请先添加标准问题"）
- [ ] 只有 CANDIDATE 数据集时运行评估（应提示"请设置为有效状态"）
- [ ] 有 5-10 个 ACTIVE 数据集时运行评估（应成功运行，1-2分钟内完成）
- [ ] 有 20+ 个 ACTIVE 数据集时运行评估（查看是否超时）
- [ ] 浏览器控制台显示详细错误信息
- [ ] 后端日志包含结构化信息

## 后续建议

如问题仍未解决，请：
1. 收集浏览器控制台完整错误
2. 收集后端日志（最近100行）
3. 运行 `debug_evaluation.py` 并保存输出
4. 查阅[完整故障排查指南](../troubleshooting/evaluation-run-error.md)

## 长期优化方向

当前的同步评估模式存在性能瓶颈，建议实施 **异步任务模式**：
- 将评估改为后台任务
- 支持任意数量的问题
- 显示实时进度
- 用户可以关闭页面，任务继续运行

详见：[超时问题修复文档 - 长期优化方案](evaluation-timeout-fix.md#长期优化方案建议)
