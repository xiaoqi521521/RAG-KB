from __future__ import annotations

from fastapi import HTTPException, status

from app.core.context import CurrentUser
from app.models import EvalDataset, EvalDatasetStatus
from app.repositories.evaluations import CurrentChunkSummary, EvaluationRepository
from app.schemas.evaluation import EvalDatasetWriteRequest


class EvaluationDatasetService:
    """维护标准问题生命周期并校验期望 chunk 标注。"""

    def __init__(self, repository: EvaluationRepository) -> None:
        self.repository = repository

    async def list_datasets(
        self,
        *,
        kb_id: int,
        status_filter: EvalDatasetStatus | None = None,
    ) -> list[EvalDataset]:
        """列出知识库下全部或指定状态的标准问题。"""
        return await self.repository.list_datasets(
            kb_id=kb_id,
            status=status_filter.value if status_filter is not None else None,
        )

    async def create_dataset(
        self,
        *,
        kb_id: int,
        request: EvalDatasetWriteRequest,
        user: CurrentUser,
    ) -> EvalDataset:
        """创建默认处于 ACTIVE 的人工标准问题（可通过 status 字段自定义）。"""
        await self._validate_chunk_ids(kb_id, request.expected_chunk_ids)
        status = request.status or EvalDatasetStatus.ACTIVE.value
        return await self.repository.create_dataset(
            kb_id=kb_id,
            question=request.question,
            expected_answer=request.expected_answer,
            expected_chunk_ids=request.expected_chunk_ids,
            status=status,
            created_by=user.user_id,
        )

    async def update_dataset(
        self,
        *,
        kb_id: int,
        dataset_id: int,
        request: EvalDatasetWriteRequest,
    ) -> EvalDataset:
        """编辑未参与评估且未归档的标准问题（可更新状态）。"""
        dataset = await self._get_dataset(kb_id, dataset_id)
        if dataset.status == EvalDatasetStatus.ARCHIVED.value:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="已归档标准问题不能编辑",
            )
        if await self.repository.has_results(dataset_id):
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="已参与评估的标准问题不能原地修改，请归档后新建",
            )

        await self._validate_chunk_ids(kb_id, request.expected_chunk_ids)
        dataset.question = request.question
        dataset.expected_answer = request.expected_answer
        dataset.expected_chunk_ids = request.expected_chunk_ids
        if request.status is not None:
            dataset.status = request.status
        dataset.review_reason = None
        return await self.repository.save_dataset(dataset)

    async def archive_dataset(self, *, kb_id: int, dataset_id: int) -> EvalDataset:
        """幂等归档标准问题，不删除历史记录。"""
        dataset = await self._get_dataset(kb_id, dataset_id)
        if dataset.status != EvalDatasetStatus.ARCHIVED.value:
            dataset.status = EvalDatasetStatus.ARCHIVED.value
            dataset.review_reason = None
            await self.repository.save_dataset(dataset)
        return dataset

    async def list_current_chunks(self, *, kb_id: int) -> list[CurrentChunkSummary]:
        """列出可用于当前标准问题标注的 chunk 摘要。"""
        return await self.repository.list_current_chunk_summaries(kb_id=kb_id)

    async def _get_dataset(self, kb_id: int, dataset_id: int) -> EvalDataset:
        """在知识库范围内读取标准问题，跨范围统一表现为不存在。"""
        dataset = await self.repository.get_dataset(kb_id=kb_id, dataset_id=dataset_id)
        if dataset is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="标准问题不存在")
        return dataset

    async def _validate_chunk_ids(self, kb_id: int, chunk_ids: list[int] | None) -> None:
        """确保全部期望 chunk 都属于目标知识库的当前已发布版本。"""
        if chunk_ids is None:
            return
        valid_ids = await self.repository.list_valid_chunk_ids(
            kb_id=kb_id,
            chunk_ids=chunk_ids,
        )
        if set(valid_ids) != set(chunk_ids):
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail="期望 Chunk 不属于当前知识库的有效文档版本",
            )
