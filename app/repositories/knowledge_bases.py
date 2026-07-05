from __future__ import annotations

from collections.abc import Iterable

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import KnowledgeBase


class KnowledgeBaseRepository:
    """知识库主表数据访问层。"""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def create(
        self,
        *,
        name: str,
        description: str | None,
        department_id: str,
        is_public: bool,
        created_by: int,
    ) -> KnowledgeBase:
        """创建知识库主记录并返回持久化后的实体。"""
        knowledge_base = KnowledgeBase(
            name=name,
            description=description,
            department_id=department_id,
            is_public=is_public,
            created_by=created_by,
        )
        self.session.add(knowledge_base)
        await self.session.flush()
        return knowledge_base

    async def get(self, kb_id: int) -> KnowledgeBase | None:
        """按知识库主键获取单条记录。"""
        return await self.session.get(KnowledgeBase, kb_id)

    async def list_not_deleted(self) -> list[KnowledgeBase]:
        """列出所有未删除的知识库。"""
        result = await self.session.execute(
            select(KnowledgeBase).where(KnowledgeBase.is_deleted.is_(False))
        )
        return list(result.scalars().all())

    async def list_public(self) -> list[KnowledgeBase]:
        """列出所有未删除且公开可见的知识库。"""
        result = await self.session.execute(
            select(KnowledgeBase).where(
                KnowledgeBase.is_deleted.is_(False),
                KnowledgeBase.is_public.is_(True),
            )
        )
        return list(result.scalars().all())

    async def list_by_ids(self, kb_ids: Iterable[int]) -> list[KnowledgeBase]:
        """按知识库 ID 集合批量查询未删除的记录。"""
        ids = list(kb_ids)
        if not ids:
            return []
        result = await self.session.execute(
            select(KnowledgeBase).where(
                KnowledgeBase.id.in_(ids),
                KnowledgeBase.is_deleted.is_(False),
            )
        )
        return list(result.scalars().all())
