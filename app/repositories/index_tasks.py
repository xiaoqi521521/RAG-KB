from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy.ext.asyncio import AsyncSession

from app.models import IndexTask, IndexTaskStatus, IndexTaskType


def _utcnow_naive() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


class IndexTaskRepository:
    """`kb_index_task` 的最小索引阶段数据访问层。"""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def create(
        self,
        doc_id: int,
        *,
        task_type: IndexTaskType = IndexTaskType.INDEX,
    ) -> IndexTask:
        task = IndexTask(
            doc_id=doc_id,
            task_type=task_type.value,
            status=IndexTaskStatus.PENDING.value,
        )
        self.session.add(task)
        await self.session.flush()
        return task

    async def get(self, task_id: int) -> IndexTask | None:
        return await self.session.get(IndexTask, task_id)

    async def mark_running(self, task_id: int) -> None:
        task = await self.get(task_id)
        if task is None:
            return
        task.status = IndexTaskStatus.RUNNING.value
        task.started_at = _utcnow_naive()
        task.error_msg = None
        await self.session.flush()

    async def mark_done(self, task_id: int) -> None:
        task = await self.get(task_id)
        if task is None:
            return
        task.status = IndexTaskStatus.DONE.value
        task.error_msg = None
        task.finished_at = _utcnow_naive()
        await self.session.flush()

    async def mark_failed(self, task_id: int, error_msg: str) -> None:
        task = await self.get(task_id)
        if task is None:
            return
        task.status = IndexTaskStatus.FAILED.value
        task.error_msg = error_msg
        task.finished_at = _utcnow_naive()
        await self.session.flush()

    async def mark_retry_pending(
        self,
        task_id: int,
        *,
        retry_count: int,
        error_msg: str,
    ) -> None:
        task = await self.get(task_id)
        if task is None:
            return
        task.status = IndexTaskStatus.PENDING.value
        task.retry_count = retry_count
        task.error_msg = error_msg
        task.finished_at = None
        await self.session.flush()
