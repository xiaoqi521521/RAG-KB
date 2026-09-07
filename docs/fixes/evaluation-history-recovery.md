# 评估历史恢复说明

## 问题描述

评估管理页面的"评估历史"部分显示"暂无评估记录"，但实际上 `kb_eval_result` 表中存在历史评估数据。

## 根本原因

之前运行的脚本 `scripts/add_eval_dataset.py` 使用了 **硬删除（DELETE）** 操作，导致以下数据丢失：

- `kb_eval_dataset` 表中 `id=1` 和 `id=2` 的记录被永久删除
- 但 `kb_eval_result` 表中仍保留着这些数据集的评估结果记录
- 评估历史查询通过 JOIN 关联两个表：
  ```sql
  SELECT ...
  FROM kb_eval_result
  JOIN kb_eval_dataset ON kb_eval_result.dataset_id = kb_eval_dataset.id
  WHERE kb_eval_dataset.kb_id = 1
  ```
- 由于数据集记录被删除，JOIN 无法匹配，导致查询结果为空

## 解决方案

从数据库备份文件 `app/db/ragkb_full_dump.sql` 中恢复被删除的数据集记录，并设置为 **ARCHIVED** 状态，保留历史数据完整性。

### 恢复的数据

| ID | KB_ID | 问题 | 状态 | 评估结果版本 |
|----|-------|------|------|--------------|
| 1  | 1     | 新员工入职第一天需要做什么？ | ARCHIVED | 1-6 |
| 2  | 1     | 年假是怎么规定的？ | ARCHIVED | 1-6 |

### 执行步骤

1. 创建恢复脚本：`scripts/restore_archived_eval_data.py`
2. 运行脚本恢复数据：
   ```bash
   python scripts/restore_archived_eval_data.py
   ```
3. 验证结果：
   - KB_ID=1 现在有 19 条评估数据集（2 条 ARCHIVED + 17 条 ACTIVE）
   - 评估历史显示 6 个版本（版本 1-6）

## 验证结果

### 评估历史查询结果

```
版本 6: 题量=2, 成功=2, 时间=2026-07-16 20:10:34
版本 5: 题量=2, 成功=0, 时间=2026-07-16 19:35:41
版本 4: 题量=2, 成功=1, 时间=2026-07-16 19:05:06
版本 3: 题量=2, 成功=1, 时间=2026-07-16 19:01:58
版本 2: 题量=2, 成功=0, 时间=2026-07-16 18:05:40
版本 1: (存在)
```

### 数据集状态

- **ARCHIVED（2条）**: 旧的测试数据，不参与新评估
- **ACTIVE（17条）**: 基于 policy.pdf 的新评估数据集

## 前端显示

恢复后，评估管理页面的"评估历史"部分将正常显示：

- ✅ 显示 6 个历史评估版本
- ✅ 显示每个版本的指标（命中率、MRR、忠实度等）
- ✅ 版本筛选下拉框正常工作
- ✅ 分页功能正常

## 经验教训

### ❌ 错误做法：硬删除
```python
# 错误：永久删除数据
await conn.execute(text("DELETE FROM kb_eval_dataset WHERE kb_id = 1"))
```

### ✅ 正确做法：软删除（归档）
```python
# 正确：保留数据并标记为已归档
await conn.execute(text("""
    UPDATE kb_eval_dataset
    SET status = 'ARCHIVED'
    WHERE kb_id = 1 AND status != 'ARCHIVED'
"""))
```

## 数据完整性

现在系统中的数据关系：

```
kb_eval_dataset (19条)
├── ARCHIVED (2条)
│   ├── id=1 ─┐
│   └── id=2 ─┤
│             └─> kb_eval_result (12条，版本1-6的结果)
└── ACTIVE (17条)
    └── id=39-55 (尚未参与评估)
```

## 后续建议

1. **更新所有数据管理脚本**：使用 `UPDATE status='ARCHIVED'` 而不是 `DELETE`
2. **添加外键约束**：防止孤立记录
   ```sql
   ALTER TABLE kb_eval_result
   ADD CONSTRAINT fk_eval_result_dataset
   FOREIGN KEY (dataset_id) REFERENCES kb_eval_dataset(id)
   ON DELETE CASCADE;
   ```
3. **定期备份**：在执行数据修改脚本前先备份数据库

## 相关文件

- 恢复脚本：`scripts/restore_archived_eval_data.py`
- 问题脚本：`scripts/add_eval_dataset.py`（已修复，不再使用 DELETE）
- 改进脚本：`scripts/update_eval_dataset_for_policy.py`（使用 ARCHIVED 状态）
- 数据库备份：`app/db/ragkb_full_dump.sql`

## 总结

通过恢复被硬删除的数据集记录并设置为 ARCHIVED 状态，成功解决了评估历史显示为空的问题。现在前端可以正常查看历史评估记录，同时保持了数据完整性和可追溯性。
