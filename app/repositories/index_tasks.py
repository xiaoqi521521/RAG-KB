from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.time import shanghai_now_naive
from app.models import IndexTask, IndexTaskStatus, IndexTaskType


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
        """为指定文档创建一条待执行的索引任务。"""
        task = IndexTask(
            doc_id=doc_id,
            task_type=task_type.value,
            status=IndexTaskStatus.PENDING.value,
        )
        self.session.add(task)
        await self.session.flush()
        return task

    async def get(self, task_id: int) -> IndexTask | None:
        """按任务主键获取单条索引任务。"""
        return await self.session.get(IndexTask, task_id)

    async def get_latest_by_doc_id(self, doc_id: int) -> IndexTask | None:
        """获取指定文档最新创建的一条索引任务。"""
        result = await self.session.execute(
            select(IndexTask)
            .where(IndexTask.doc_id == doc_id)
            .order_by(IndexTask.created_at.desc(), IndexTask.id.desc())
            .limit(1)
        )
        return result.scalar_one_or_none()

    async def mark_running(self, task_id: int) -> None:
        """将索引任务状态更新为运行中并记录开始时间。"""
        task = await self.get(task_id)
        if task is None:
            return
        task.status = IndexTaskStatus.RUNNING.value
        task.started_at = shanghai_now_naive()
        task.error_msg = None
        await self.session.flush()

    async def mark_done(self, task_id: int) -> None:
        """将索引任务状态更新为完成并记录结束时间。"""
        task = await self.get(task_id)
        if task is None:
            return
        task.status = IndexTaskStatus.DONE.value
        task.error_msg = None
        task.finished_at = shanghai_now_naive()
        await self.session.flush()

    async def mark_failed(self, task_id: int, error_msg: str) -> None:
        """将索引任务状态更新为失败，并保留错误信息。"""
        task = await self.get(task_id)
        if task is None:
            return
        task.status = IndexTaskStatus.FAILED.value
        task.error_msg = error_msg
        task.finished_at = shanghai_now_naive()
        await self.session.flush()

    async def mark_retry_pending(
        self,
        task_id: int,
        *,
        retry_count: int,
        error_msg: str,
    ) -> None:
        """将索引任务重置为待重试，并更新重试次数。"""
        task = await self.get(task_id)
        if task is None:
            return
        task.status = IndexTaskStatus.PENDING.value
        task.retry_count = retry_count
        task.error_msg = error_msg
        task.finished_at = None
        await self.session.flush()
