# 评估数据集状态管理功能

## 功能概述

评估管理模块现在支持在创建和编辑标准问题时设置和修改评估数据集的状态。

## 可用状态

评估数据集支持以下四种状态：

- **ACTIVE（有效）**: 活跃的评估问题，会被包含在评估运行中
- **CANDIDATE（候选）**: 候选问题，待审核后激活
- **NEEDS_REVIEW（待审核）**: 需要人工审核的问题
- **ARCHIVED（已归档）**: 已归档的历史问题，不参与评估

## 后端修改

### 1. Schema 更新 (`app/schemas/evaluation.py`)

`EvalDatasetWriteRequest` 添加了可选的 `status` 字段：

```python
class EvalDatasetWriteRequest(BaseModel):
    question: str
    expected_answer: str | None = None
    expected_chunk_ids: list[PositiveId] | None = None
    status: str | None = None  # 新增字段
    
    @field_validator("status")
    @classmethod
    def validate_status(cls, value: str | None) -> str | None:
        """校验状态值合法性。"""
        if value is None:
            return None
        if value not in {"CANDIDATE", "ACTIVE", "NEEDS_REVIEW", "ARCHIVED"}:
            raise ValueError("invalid status value")
        return value
```

### 2. Service 更新 (`app/evaluation/dataset_service.py`)

#### 创建数据集
- 如果请求中包含 `status`，使用指定的状态
- 否则默认为 `ACTIVE`

```python
async def create_dataset(
    self,
    *,
    kb_id: int,
    request: EvalDatasetWriteRequest,
    user: CurrentUser,
) -> EvalDataset:
    """创建默认处于 ACTIVE 的人工标准问题（可通过 status 字段自定义）。"""
    status = request.status or EvalDatasetStatus.ACTIVE.value
    return await self.repository.create_dataset(
        kb_id=kb_id,
        question=request.question,
        expected_answer=request.expected_answer,
        expected_chunk_ids=request.expected_chunk_ids,
        status=status,
        created_by=user.user_id,
    )
```

#### 更新数据集
- 如果请求中包含 `status`，更新为新状态
- 否则保持原状态不变

```python
async def update_dataset(
    self,
    *,
    kb_id: int,
    dataset_id: int,
    request: EvalDatasetWriteRequest,
) -> EvalDataset:
    """编辑未参与评估且未归档的标准问题（可更新状态）。"""
    dataset = await self._get_dataset(kb_id, dataset_id)
    # ... 校验逻辑 ...
    
    dataset.question = request.question
    dataset.expected_answer = request.expected_answer
    dataset.expected_chunk_ids = request.expected_chunk_ids
    if request.status is not None:
        dataset.status = request.status  # 更新状态
    dataset.review_reason = None
    return await self.repository.save_dataset(dataset)
```

## 前端修改

### 1. 类型定义更新 (`frontend/src/types/index.ts`)

```typescript
export interface EvalDatasetWriteRequest {
  question: string;
  expected_answer?: string | null;
  expected_chunk_ids?: number[] | null;
  status?: string | null;  // 新增字段
}
```

### 2. 表单更新 (`frontend/src/pages/Evaluation.tsx`)

#### 表单字段
添加了状态选择器表单项：

```tsx
<Form.Item
  name="status"
  label="状态"
  initialValue="ACTIVE"
  extra="设置评估数据集的状态：有效、候选、待审核或已归档"
>
  <Select
    options={[
      { label: '有效', value: 'ACTIVE' },
      { label: '候选', value: 'CANDIDATE' },
      { label: '待审核', value: 'NEEDS_REVIEW' },
      { label: '已归档', value: 'ARCHIVED' },
    ]}
    placeholder="选择状态"
  />
</Form.Item>
```

#### 数据处理
- 创建时：发送用户选择的状态（默认 ACTIVE）
- 编辑时：预填充当前状态，支持修改

## API 接口

### 创建评估数据集
```
POST /api/v1/evaluation/{kb_id}/dataset
```

请求体：
```json
{
  "question": "如何申请 API 访问权限？",
  "expected_answer": "在开发者控制台创建 API Key...",
  "expected_chunk_ids": [214, 215],
  "status": "ACTIVE"
}
```

### 更新评估数据集
```
PUT /api/v1/evaluation/{kb_id}/dataset/{dataset_id}
```

请求体：
```json
{
  "question": "如何申请 API 访问权限？",
  "expected_answer": "在开发者控制台创建 API Key...",
  "expected_chunk_ids": [214, 215],
  "status": "NEEDS_REVIEW"
}
```

## 使用场景

1. **标记候选问题**：将新创建的问题标记为 `CANDIDATE`，待审核后再激活
2. **暂时禁用问题**：将有问题的评估数据标记为 `NEEDS_REVIEW`，修复后再激活
3. **归档历史数据**：将过时的问题标记为 `ARCHIVED`，保留历史记录但不参与评估
4. **快速激活**：直接创建 `ACTIVE` 状态的问题，立即参与评估

## 向后兼容性

- 如果客户端不发送 `status` 字段，默认行为保持不变（创建为 ACTIVE）
- 现有的 API 调用不受影响
- 数据库约束确保状态值始终有效

## 测试

所有现有测试通过，包括：
- 数据集管理和权限测试
- 写入请求字段验证
- 评估运行和历史记录测试
- TypeScript 类型检查

## 截图

编辑标准问题对话框现在包含状态选择器：

```
┌─────────────────────────────────────────┐
│ 编辑标准问题                      × │
├─────────────────────────────────────────┤
│ 问题                                    │
│ ┌─────────────────────────────────────┐ │
│ │ 如何申请 API 访问权限？              │ │
│ └─────────────────────────────────────┘ │
│                                         │
│ 期望答案                                │
│ ┌─────────────────────────────────────┐ │
│ │ 在开发者控制台创建 API Key...        │ │
│ └─────────────────────────────────────┘ │
│                                         │
│ 目标 chunk                              │
│ ┌─────────────────────────────────────┐ │
│ │ 选择目标 chunk               ▼      │ │
│ └─────────────────────────────────────┘ │
│                                         │
│ 状态                                    │
│ ┌─────────────────────────────────────┐ │
│ │ 有效                         ▼      │ │  <-- 新增
│ └─────────────────────────────────────┘ │
│ 设置评估数据集的状态：有效、候选、      │
│ 待审核或已归档                          │
│                                         │
│              [ 取消 ]  [ 保存 ]         │
└─────────────────────────────────────────┘
```
