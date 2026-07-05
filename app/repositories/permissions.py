from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import KbPermission, KbPermissionLevel, PermissionSubjectType


class KbPermissionRepository:
    """知识库权限表数据访问层。"""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def create_admin_permission(self, kb_id: int, user_id: int) -> KbPermission:
        """为知识库创建一条用户级管理员权限记录。"""
        permission = KbPermission(
            kb_id=kb_id,
            subject_type=PermissionSubjectType.USER.value,
            subject_id=str(user_id),
            permission=KbPermissionLevel.ADMIN.value,
            granted_by=user_id,
        )
        self.session.add(permission)
        await self.session.flush()
        return permission

    async def get_permission(self, kb_id: int, subject_type: str, subject_id: str) -> str | None:
        """查询指定主体在知识库上的权限等级。"""
        result = await self.session.execute(
            select(KbPermission.permission).where(
                KbPermission.kb_id == kb_id,
                KbPermission.subject_type == subject_type,
                KbPermission.subject_id == subject_id,
            )
        )
        return result.scalar_one_or_none()

    async def list_by_subject(self, subject_type: str, subject_id: str) -> list[KbPermission]:
        """列出指定主体关联的全部知识库权限记录。"""
        result = await self.session.execute(
            select(KbPermission).where(
                KbPermission.subject_type == subject_type,
                KbPermission.subject_id == subject_id,
            )
        )
        return list(result.scalars().all())
