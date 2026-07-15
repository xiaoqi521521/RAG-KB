from __future__ import annotations

import time

from fastapi import HTTPException, status
from sqlalchemy.exc import DBAPIError

from app.core.context import CurrentUser
from app.models import KbPermissionLevel, KnowledgeBase, PermissionSubjectType
from app.repositories.knowledge_bases import KnowledgeBaseRepository
from app.repositories.permissions import KbPermissionRepository
from app.services.permission_metrics import PermissionMetrics


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
        metrics: PermissionMetrics | None = None,
    ) -> None:
        self.knowledge_base_repository = knowledge_base_repository
        self.permission_repository = permission_repository
        self.metrics = metrics or PermissionMetrics()

    async def require_read(self, kb_id: int, user: CurrentUser) -> None:
        """校验当前用户是否具备知识库读权限。"""
        await self._require(kb_id=kb_id, user=user, action="read")

    async def require_write(self, kb_id: int, user: CurrentUser) -> None:
        """校验当前用户是否具备知识库写权限。"""
        await self._require(kb_id=kb_id, user=user, action="write")

    async def require_admin(self, kb_id: int, user: CurrentUser) -> None:
        """校验系统管理员或目标知识库的本地管理员权限。"""
        await self._require(kb_id=kb_id, user=user, action="admin")

    async def get_highest_permission(self, kb_id: int, user: CurrentUser) -> str | None:
        """合并用户权限和部门权限，返回最高权限级别。"""
        try:
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
        except DBAPIError as exc:
            raise self._service_unavailable() from exc
        return self._higher_permission(user_permission, department_permission)

    async def list_accessible(self, user: CurrentUser) -> list[KnowledgeBase]:
        """列出当前用户可读的未删除知识库。"""
        try:
            if user.is_admin:
                return await self.knowledge_base_repository.list_not_deleted()

            permissions_by_kb: dict[int, str] = {}
            for permission in await self.permission_repository.list_by_subject(
                PermissionSubjectType.DEPARTMENT.value,
                user.department_id,
            ):
                permissions_by_kb[permission.kb_id] = self._higher_permission(
                    permissions_by_kb.get(permission.kb_id),
                    permission.permission,
                ) or permission.permission

            for permission in await self.permission_repository.list_by_subject(
                PermissionSubjectType.USER.value,
                str(user.user_id),
            ):
                permissions_by_kb[permission.kb_id] = self._higher_permission(
                    permissions_by_kb.get(permission.kb_id),
                    permission.permission,
                ) or permission.permission

            public_kbs = await self.knowledge_base_repository.list_public()
            explicit_kbs = await self.knowledge_base_repository.list_by_ids(permissions_by_kb)
        except DBAPIError as exc:
            raise self._service_unavailable() from exc

        merged = {knowledge_base.id: knowledge_base for knowledge_base in explicit_kbs}
        merged.update({knowledge_base.id: knowledge_base for knowledge_base in public_kbs})
        return list(merged.values())

    async def _get_knowledge_base(self, kb_id: int):
        """读取知识库，并将权限数据源故障转换为拒绝式错误。"""
        try:
            return await self.knowledge_base_repository.get(kb_id)
        except DBAPIError as exc:
            raise self._service_unavailable() from exc

    async def _require(self, *, kb_id: int, user: CurrentUser, action: str) -> None:
        """执行读写权限检查并记录不含敏感标识的观测结果。"""
        started_at = time.perf_counter()
        result = "denied"
        source = "none"
        try:
            knowledge_base = await self._get_knowledge_base(kb_id)
            if knowledge_base is None or knowledge_base.is_deleted:
                result = "not_found"
                source = "resource"
                raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="知识库不存在")

            if user.is_admin:
                result = "allowed"
                source = "system_admin"
                return

            if action == "read" and knowledge_base.is_public:
                result = "allowed"
                source = "public"
                return

            permission = await self.get_highest_permission(kb_id, user)
            if self._has_required_permission(permission, action):
                result = "allowed"
                source = "grant"
                return

            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail={
                    "read": "无权访问该知识库",
                    "write": "无文档管理权限",
                    "admin": "无评估管理权限",
                }[action],
            )
        except HTTPException as exc:
            if exc.status_code == status.HTTP_503_SERVICE_UNAVAILABLE:
                result = "unavailable"
                source = "dependency"
            raise
        finally:
            elapsed_ms = max(0, int((time.perf_counter() - started_at) * 1000))
            self.metrics.record(action=action, result=result, source=source, elapsed_ms=elapsed_ms)

    def _has_required_permission(self, permission: str | None, action: str) -> bool:
        """判断有效权限能否完成当前读写操作。"""
        if action == "read":
            return permission is not None
        if action == "admin":
            return permission == KbPermissionLevel.ADMIN.value
        return permission in {KbPermissionLevel.WRITE.value, KbPermissionLevel.ADMIN.value}

    def _service_unavailable(self) -> HTTPException:
        """创建权限数据源不可用时的统一错误。"""
        return HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="权限服务暂不可用",
        )

    def _higher_permission(self, left: str | None, right: str | None) -> str | None:
        if left is None:
            return right
        if right is None:
            return left
        return left if self._level_order.get(left, 0) >= self._level_order.get(right, 0) else right
