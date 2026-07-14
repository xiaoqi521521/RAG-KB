from __future__ import annotations

from dataclasses import dataclass

from app.core.context import CurrentUser
from app.schemas.rag import SourceCitation
from app.services.rag_query_v4 import PreparedRagContext
from app.services.synchronous_chat import SynchronousChatService


@dataclass
class FakeMessage:
    content: str
    usage_metadata: dict[str, int] | None = None


class FakeTokenMetrics:
    async def record_generation_tokens(self, *, tokens: int, source: str = "provider") -> None:
        return None


class FakeSessionContext:
    async def __aenter__(self) -> object:
        return object()

    async def __aexit__(self, *args: object) -> None:
        return None


class FakeSessionFactory:
    def __call__(self) -> FakeSessionContext:
        return FakeSessionContext()


class FakeChatModel:
    async def ainvoke(self, messages: list[object]) -> FakeMessage:
        return FakeMessage(content="根据员工手册。", usage_metadata={"output_tokens": 4})


class FakeRagService:
    def __init__(self, prepared_context: PreparedRagContext | None) -> None:
        self.prepared_context = prepared_context
        self.chat_model = FakeChatModel()
        self.token_metrics = FakeTokenMetrics()
        self.received_history: list[object] | None = None

    async def prepare_context(self, **kwargs: object) -> PreparedRagContext | None:
        return self.prepared_context

    def build_generation_messages(
        self,
        *,
        question: str,
        prepared_context: PreparedRagContext,
        history: list[object] | None = None,
    ) -> list[object]:
        self.received_history = history
        return [*(history or []), question]

    def finalize_answer(self, **kwargs: object) -> list[SourceCitation] | None:
        return self.prepared_context.sources if self.prepared_context is not None else None


class InMemorySynchronousChatService(SynchronousChatService):
    def __init__(self, rag_service: FakeRagService) -> None:
        super().__init__(
            session_factory=FakeSessionFactory(),
            rag_service_factory=lambda session: rag_service,
        )
        self.history: list[object] = ["earlier-user", "earlier-assistant"]
        self.saved_turns: list[dict[str, object]] = []

    async def _get_or_create_session(self, **kwargs: object) -> str:
        return "session-1"

    async def _load_history(self, session_id: str, user: CurrentUser) -> list[object]:
        return self.history

    async def _save_turn(self, **kwargs: object) -> None:
        self.saved_turns.append(kwargs)


def _prepared_context() -> PreparedRagContext:
    return PreparedRagContext(
        context="员工手册中的年假规则。",
        sources=[
            SourceCitation(
                reference_index=1,
                document_id=1,
                document_name="员工手册.pdf",
                kb_id=2,
                chunk_id=10,
                chunk_index=0,
                page_number=12,
                section_title=None,
                excerpt="年假规则",
                score=0.9,
            )
        ],
    )


async def test_query_injects_history_and_saves_complete_turn() -> None:
    rag_service = FakeRagService(_prepared_context())
    service = InMemorySynchronousChatService(rag_service)

    response = await service.query(
        question="年假怎么申请？",
        kb_ids=[2],
        session_id="session-1",
        user=CurrentUser(user_id=1, department_id="engineering", role="ADMIN"),
    )

    assert response.session_id == "session-1"
    assert response.answer == "根据员工手册。"
    assert response.hit_count == 1
    assert rag_service.received_history == ["earlier-user", "earlier-assistant"]
    assert len(service.saved_turns) == 1
    assert service.saved_turns[0]["question"] == "年假怎么申请？"
    assert service.saved_turns[0]["answer"] == "根据员工手册。"
    assert service.saved_turns[0]["token_count"] == 4


async def test_query_returns_refusal_without_saving_turn_when_context_is_missing() -> None:
    service = InMemorySynchronousChatService(FakeRagService(None))

    response = await service.query(
        question="年假怎么申请？",
        kb_ids=[2],
        session_id=None,
        user=CurrentUser(user_id=1, department_id="engineering", role="ADMIN"),
    )

    assert response.session_id == "session-1"
    assert response.sources == []
    assert response.hit_count == 0
    assert service.saved_turns == []
