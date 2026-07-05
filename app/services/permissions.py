from __future__ import annotations

from fastapi import HTTPException, status

from app.core.context import CurrentUser
from app.models import KbPermissionLevel, PermissionSubjectType
from app.repositories.knowledge_bases import KnowledgeBaseRepository
from app.repositories.permissions import KbPermissionRepository


class PermissionService:
    """知识库读写权限校验服务。"""

    _level_order = {
        KbPermissionLevel.READ.value: 1,
        KbPermissionLevel.WRITE.value: 2,
        KbPermissionLevel.ADMIN.value: 3,
    }

    def __init__(
        self,
        *,
        knowledge_base_repository: KnowledgeBaseRepository,
        permission_repository: KbPermissionRepository,
    ) -> None:
        self.knowledge_base_repository = knowledge_base_repository
        self.permission_repository = permission_repository

    async def require_read(self, kb_id: int, user: CurrentUser) -> None:
        """校验当前用户是否具备知识库读权限。"""
        knowledge_base = await self.knowledge_base_repository.get(kb_id)
        if knowledge_base is None or knowledge_base.is_deleted:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="知识库不存在")

        if user.is_admin or knowledge_base.is_public:
            return

        permission = await self.get_highest_permission(kb_id, user)
        if permission is None:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="无权访问该知识库")

    async def require_write(self, kb_id: int, user: CurrentUser) -> None:
        """校验当前用户是否具备知识库写权限。"""
        knowledge_base = await self.knowledge_base_repository.get(kb_id)
        if knowledge_base is None or knowledge_base.is_deleted:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="知识库不存在")

        if user.is_admin:
            return

        permission = await self.get_highest_permission(kb_id, user)
        if permission not in {KbPermissionLevel.WRITE.value, KbPermissionLevel.ADMIN.value}:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="无文档管理权限")

    async def get_highest_permission(self, kb_id: int, user: CurrentUser) -> str | None:
        """合并用户权限和部门权限，返回最高权限级别。"""
        user_permission = await self.permission_repository.get_permission(
            kb_id,
            PermissionSubjectType.USER.value,
            str(user.user_id),
        )
        department_permission = await self.permission_repository.get_permission(
            kb_id,
            PermissionSubjectType.DEPARTMENT.value,
            user.department_id,
        )
        return self._higher_permission(user_permission, department_permission)

    def _higher_permission(self, left: str | None, right: str | None) -> str | None:
        if left is None:
            return right
        if right is None:
            return left
        return left if self._level_order.get(left, 0) >= self._level_order.get(right, 0) else right
