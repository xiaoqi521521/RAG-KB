from __future__ import annotations

import pytest
from fastapi import HTTPException

from app.core.context import CurrentUser
from app.models import KbPermissionLevel, KnowledgeBase, PermissionSubjectType
from app.services.permissions import PermissionService


def _user(
    *,
    user_id: int = 1,
    department_id: str = "engineering",
    role: str = "USER",
) -> CurrentUser:
    return CurrentUser(
        user_id=user_id,
        department_id=department_id,
        role=role,
    )


class FakeKnowledgeBaseRepository:
    def __init__(self, knowledge_bases: list[KnowledgeBase]) -> None:
        self.knowledge_bases = {kb.id: kb for kb in knowledge_bases}

    async def get(self, kb_id: int) -> KnowledgeBase | None:
        return self.knowledge_bases.get(kb_id)


class FakePermissionRepository:
    def __init__(self, permissions: dict[tuple[int, str, str], str]) -> None:
        self.permissions = permissions

    async def get_permission(self, kb_id: int, subject_type: str, subject_id: str) -> str | None:
        return self.permissions.get((kb_id, subject_type, subject_id))


def _service(
    *,
    knowledge_bases: list[KnowledgeBase] | None = None,
    permissions: dict[tuple[int, str, str], str] | None = None,
) -> PermissionService:
    return PermissionService(
        knowledge_base_repository=FakeKnowledgeBaseRepository(
            knowledge_bases or [KnowledgeBase(id=1, name="KB", department_id="engineering", created_by=1)]
        ),
        permission_repository=FakePermissionRepository(permissions or {}),
    )


@pytest.mark.asyncio
async def test_require_read_allows_public_knowledge_base() -> None:
    service = _service(
        knowledge_bases=[
            KnowledgeBase(
                id=1,
                name="Public KB",
                department_id="engineering",
                is_public=True,
                created_by=1,
            )
        ]
    )

    await service.require_read(1, _user())


@pytest.mark.asyncio
async def test_require_write_rejects_public_knowledge_base_without_write_permission() -> None:
    service = _service(
        knowledge_bases=[
            KnowledgeBase(
                id=1,
                name="Public KB",
                department_id="engineering",
                is_public=True,
                created_by=1,
            )
        ]
    )

    with pytest.raises(HTTPException) as exc_info:
        await service.require_write(1, _user())

    assert exc_info.value.status_code == 403


@pytest.mark.asyncio
async def test_require_write_allows_user_write_permission() -> None:
    service = _service(
        permissions={
            (
                1,
                PermissionSubjectType.USER.value,
                "7",
            ): KbPermissionLevel.WRITE.value
        }
    )

    await service.require_write(1, _user(user_id=7))


@pytest.mark.asyncio
async def test_get_highest_permission_prefers_user_admin_over_department_read() -> None:
    service = _service(
        permissions={
            (
                1,
                PermissionSubjectType.DEPARTMENT.value,
                "engineering",
            ): KbPermissionLevel.READ.value,
            (
                1,
                PermissionSubjectType.USER.value,
                "7",
            ): KbPermissionLevel.ADMIN.value,
        }
    )

    permission = await service.get_highest_permission(1, _user(user_id=7))

    assert permission == KbPermissionLevel.ADMIN.value
