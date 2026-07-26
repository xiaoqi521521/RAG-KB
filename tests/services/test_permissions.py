from __future__ import annotations

import pytest
from fastapi import HTTPException
from sqlalchemy.exc import OperationalError

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
@pytest.mark.parametrize(
    "permission",
    [None, KbPermissionLevel.READ.value, KbPermissionLevel.WRITE.value],
)
async def test_require_admin_rejects_public_read_and_non_admin_grants(
    permission: str | None,
) -> None:
    permissions = (
        {}
        if permission is None
        else {
            (1, PermissionSubjectType.USER.value, "7"): permission,
        }
    )
    service = _service(
        knowledge_bases=[
            KnowledgeBase(
                id=1,
                name="Public KB",
                department_id="engineering",
                is_public=True,
                created_by=1,
            )
        ],
        permissions=permissions,
    )

    with pytest.raises(HTTPException) as exc_info:
        await service.require_admin(1, _user(user_id=7))

    assert exc_info.value.status_code == 403


@pytest.mark.asyncio
async def test_require_admin_allows_system_or_local_knowledge_base_admin() -> None:
    service = _service(
        permissions={
            (1, PermissionSubjectType.USER.value, "7"): KbPermissionLevel.ADMIN.value,
        }
    )

    await service.require_admin(1, _user(role="ADMIN"))
    await service.require_admin(1, _user(user_id=7))


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


@pytest.mark.asyncio
async def test_require_read_returns_service_unavailable_when_permission_store_fails() -> None:
    class UnavailablePermissionRepository(FakePermissionRepository):
        async def get_permission(self, kb_id: int, subject_type: str, subject_id: str) -> str | None:
            raise OperationalError(None, None, OSError("database unavailable"))

    service = PermissionService(
        knowledge_base_repository=FakeKnowledgeBaseRepository(
            [KnowledgeBase(id=1, name="KB", department_id="engineering", created_by=1)]
        ),
        permission_repository=UnavailablePermissionRepository({}),
    )

    with pytest.raises(HTTPException) as exc_info:
        await service.require_read(1, _user())

    assert exc_info.value.status_code == 503


@pytest.mark.asyncio
async def test_require_write_returns_service_unavailable_when_knowledge_base_store_fails() -> None:
    class UnavailableKnowledgeBaseRepository(FakeKnowledgeBaseRepository):
        async def get(self, kb_id: int) -> KnowledgeBase | None:
            raise OperationalError(None, None, OSError("database unavailable"))

    service = PermissionService(
        knowledge_base_repository=UnavailableKnowledgeBaseRepository([]),
        permission_repository=FakePermissionRepository({}),
    )

    with pytest.raises(HTTPException) as exc_info:
        await service.require_write(1, _user())

    assert exc_info.value.status_code == 503


@pytest.mark.asyncio
async def test_list_accessible_returns_service_unavailable_when_permission_store_fails() -> None:
    class UnavailablePermissionRepository(FakePermissionRepository):
        async def list_by_subject(self, subject_type: str, subject_id: str):
            raise OperationalError(None, None, OSError("database unavailable"))

    class ListableKnowledgeBaseRepository(FakeKnowledgeBaseRepository):
        async def list_public(self) -> list[KnowledgeBase]:
            return []

        async def list_by_ids(self, kb_ids: list[int]) -> list[KnowledgeBase]:
            return []

        async def list_not_deleted(self) -> list[KnowledgeBase]:
            return []

    service = PermissionService(
        knowledge_base_repository=ListableKnowledgeBaseRepository([]),
        permission_repository=UnavailablePermissionRepository({}),
    )

    with pytest.raises(HTTPException) as exc_info:
        await service.list_accessible(_user())

    assert exc_info.value.status_code == 503


@pytest.mark.asyncio
async def test_filter_readable_kb_ids_keeps_readable_scope_and_excludes_missing_or_deleted() -> None:
    service = _service(
        knowledge_bases=[
            KnowledgeBase(
                id=1,
                name="Public KB",
                department_id="engineering",
                is_public=True,
                created_by=1,
            ),
            KnowledgeBase(id=2, name="Granted KB", department_id="engineering", created_by=1),
            KnowledgeBase(
                id=3,
                name="Deleted KB",
                department_id="engineering",
                created_by=1,
                is_deleted=True,
            ),
        ],
        permissions={
            (2, PermissionSubjectType.USER.value, "7"): KbPermissionLevel.READ.value,
        },
    )

    allowed_kb_ids = await service.filter_readable_kb_ids(
        [1, 2, 3, 99],
        _user(user_id=7),
    )

    assert allowed_kb_ids == [1, 2]


@pytest.mark.asyncio
async def test_filter_readable_kb_ids_preserves_permission_store_failure() -> None:
    class UnavailablePermissionRepository(FakePermissionRepository):
        async def get_permission(self, kb_id: int, subject_type: str, subject_id: str) -> str | None:
            raise OperationalError(None, None, OSError("database unavailable"))

    service = PermissionService(
        knowledge_base_repository=FakeKnowledgeBaseRepository(
            [KnowledgeBase(id=1, name="KB", department_id="engineering", created_by=1)]
        ),
        permission_repository=UnavailablePermissionRepository({}),
    )

    with pytest.raises(HTTPException) as exc_info:
        await service.filter_readable_kb_ids([1], _user())

    assert exc_info.value.status_code == 503
