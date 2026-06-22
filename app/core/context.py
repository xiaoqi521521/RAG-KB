from contextvars import ContextVar
from dataclasses import dataclass


@dataclass(frozen=True)
class CurrentUser:
    user_id: int
    username: str
    tenant_id: str
    department_id: str
    role: str
    allowed_kb_ids: tuple[int, ...]

    @property
    def is_admin(self) -> bool:
        return self.role == "ADMIN"


current_user_var: ContextVar[CurrentUser | None] = ContextVar(
    "current_user",
    default=None,
)


def get_current_user_from_context() -> CurrentUser:
    user = current_user_var.get()
    if user is None:
        raise RuntimeError("CurrentUser is not initialized")
    return user
