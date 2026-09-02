from __future__ import annotations

from dataclasses import dataclass

from app.core.context import CurrentUser
from app.schemas.query_cache import QueryCacheEntry
from app.schemas.rag import ChatIntent, SourceCitation
from app.services.rag_query_v4 import PreparedRagContext
from app.services.source_builder import FinalizedAnswer
from app.services.synchronous_chat import SynchronousChatService


@dataclass
class FakeMessage:
    content: str
    usage_metadata: dict[str, int] | None = None


class FakeTokenMetrics:
    async def record_generation_tokens(self, *, tokens: int, source: str = "provider") -> None:
        return None


class FakeQueryCache:
    def __init__(self, entry: QueryCacheEntry | None = None) -> None:
        self.entry = entry
        self.get_calls: list[tuple[str, list[int]]] = []
        self.put_calls: list[tuple[str, list[int], object]] = []

    async def get(self, question: str, kb_ids: list[int]) -> QueryCacheEntry | None:
        self.get_calls.append((question, kb_ids))
        return self.entry

    async def put(self, question: str, kb_ids: list[int], response: object) -> None:
        self.put_calls.append((question, kb_ids, response))


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
        self.prepare_calls = 0

    async def prepare_context(self, **kwargs: object) -> PreparedRagContext | None:
        self.prepare_calls += 1
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

    def finalize_answer(self, **kwargs: object) -> FinalizedAnswer | None:
        if self.prepared_context is None:
            return None
        return FinalizedAnswer(answer=str(kwargs["answer"]), sources=self.prepared_context.sources)


class RefusingRagService(FakeRagService):
    def finalize_answer(self, **kwargs: object) -> FinalizedAnswer | None:
        return None


class InMemorySynchronousChatService(SynchronousChatService):
    def __init__(
        self,
        rag_service: FakeRagService,
        query_cache: FakeQueryCache | None = None,
    ) -> None:
        self.query_cache = query_cache or FakeQueryCache()
        super().__init__(
            session_factory=FakeSessionFactory(),
            rag_service_factory=lambda session: rag_service,
            query_cache=self.query_cache,
        )
        self.history: list[object] = ["earlier-user", "earlier-assistant"]
        self.saved_turns: list[dict[str, object]] = []
        self.session_calls: list[dict[str, object]] = []

    async def _get_or_create_session(self, **kwargs: object) -> str:
        self.session_calls.append(kwargs)
        return "session-1"

    async def _load_history(self, session_id: str, user: CurrentUser) -> list[object]:
        return self.history

    async def _save_turn(self, **kwargs: object) -> None:
        self.saved_turns.append(kwargs)


class FailingSaveSynchronousChatService(InMemorySynchronousChatService):
    async def _save_turn(self, **kwargs: object) -> None:
        raise RuntimeError("message persistence failed")


def _cache_entry() -> QueryCacheEntry:
    return QueryCacheEntry(
        version=2,
        answer="缓存回答。[参考1]",
        sources=_prepared_context().sources,
        hit_count=1,
    )


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
    assert service.saved_turns[0]["kb_ids"] == [2]
    assert service.saved_turns[0]["token_count"] == 4
    assert service.query_cache.get_calls == []
    assert service.query_cache.put_calls == []


async def test_query_saves_refusal_turn_when_context_is_missing() -> None:
    service = InMemorySynchronousChatService(FakeRagService(None))

    response = await service.query(
        question="年假怎么申请？",
        kb_ids=[2],
        session_id=None,
        user=CurrentUser(user_id=1, department_id="engineering", role="ADMIN"),
    )

    assert response.session_id == "session-1"
    assert response.answer == "在知识库中未找到与该问题相关的内容。"
    assert response.sources == []
    assert response.hit_count == 0
    assert len(service.saved_turns) == 1
    saved_turn = service.saved_turns[0]
    assert saved_turn["question"] == "年假怎么申请？"
    assert saved_turn["answer"] == "在知识库中未找到与该问题相关的内容。"
    assert saved_turn["kb_ids"] == [2]
    assert saved_turn["sources"] == []
    assert saved_turn["token_count"] == 0
    assert saved_turn["answer_mode"] == "knowledge_base"
    assert saved_turn["knowledge_base_searched"] is True
    assert service.query_cache.put_calls == []


async def test_query_saves_model_explicit_refusal_turn() -> None:
    service = InMemorySynchronousChatService(RefusingRagService(_prepared_context()))

    response = await service.query(
        question="年假怎么申请？",
        kb_ids=[2],
        session_id=None,
        user=CurrentUser(user_id=1, department_id="engineering", role="ADMIN"),
    )

    assert response.answer == "根据员工手册。"
    assert response.sources == []
    assert response.hit_count == 0
    assert len(service.saved_turns) == 1
    saved_turn = service.saved_turns[0]
    assert saved_turn["question"] == "年假怎么申请？"
    assert saved_turn["answer"] == "根据员工手册。"
    assert saved_turn["kb_ids"] == [2]
    assert saved_turn["sources"] == []
    assert saved_turn["token_count"] == 4
    assert saved_turn["answer_mode"] == "knowledge_base"
    assert saved_turn["knowledge_base_searched"] is True
    assert service.query_cache.put_calls == []


async def test_first_turn_cache_hit_saves_zero_token_turn_without_rag_call() -> None:
    rag_service = FakeRagService(_prepared_context())
    query_cache = FakeQueryCache(_cache_entry())
    service = InMemorySynchronousChatService(rag_service, query_cache)
    service.history = []

    response = await service.query(
        question="年假怎么申请？",
        kb_ids=[2],
        session_id=None,
        user=CurrentUser(user_id=1, department_id="engineering", role="ADMIN"),
    )

    assert response.answer == "缓存回答。[参考1]"
    assert response.sources == _prepared_context().sources
    assert response.hit_count == 1
    assert response.session_id == "session-1"
    assert response.latency_ms >= 0
    assert rag_service.prepare_calls == 0
    assert query_cache.get_calls == [("年假怎么申请？", [2])]
    assert query_cache.put_calls == []
    assert service.saved_turns[0]["token_count"] == 0


async def test_first_turn_cache_miss_writes_only_after_turn_persistence() -> None:
    rag_service = FakeRagService(_prepared_context())
    query_cache = FakeQueryCache()
    service = InMemorySynchronousChatService(rag_service, query_cache)
    service.history = []

    await service.query(
        question="年假怎么申请？",
        kb_ids=[2],
        session_id=None,
        user=CurrentUser(user_id=1, department_id="engineering", role="ADMIN"),
    )

    assert rag_service.prepare_calls == 1
    assert query_cache.get_calls == [("年假怎么申请？", [2])]
    assert len(query_cache.put_calls) == 1
    assert query_cache.put_calls[0][2].answer == "根据员工手册。"


async def test_non_empty_history_skips_cache_read_and_write() -> None:
    rag_service = FakeRagService(_prepared_context())
    query_cache = FakeQueryCache(_cache_entry())
    service = InMemorySynchronousChatService(rag_service, query_cache)

    await service.query(
        question="年假怎么申请？",
        kb_ids=[2],
        session_id="session-1",
        user=CurrentUser(user_id=1, department_id="engineering", role="ADMIN"),
    )

    assert rag_service.received_history == ["earlier-user", "earlier-assistant"]
    assert query_cache.get_calls == []
    assert query_cache.put_calls == []


async def test_persistence_failure_does_not_create_first_turn_cache_entry() -> None:
    query_cache = FakeQueryCache()
    service = FailingSaveSynchronousChatService(FakeRagService(_prepared_context()), query_cache)
    service.history = []

    try:
        await service.query(
            question="年假怎么申请？",
            kb_ids=[2],
            session_id=None,
            user=CurrentUser(user_id=1, department_id="engineering", role="ADMIN"),
        )
    except RuntimeError as exc:
        assert str(exc) == "message persistence failed"
    else:
        raise AssertionError("expected message persistence failure")

    assert query_cache.put_calls == []


async def test_general_chat_skips_retrieval_cache_and_saves_without_kb_scope() -> None:
    rag_service = FakeRagService(_prepared_context())
    query_cache = FakeQueryCache(_cache_entry())
    service = InMemorySynchronousChatService(rag_service, query_cache)

    response = await service.query(
        question="写一段欢迎词",
        kb_ids=[2],
        session_id=None,
        user=CurrentUser(user_id=1, department_id="engineering", role="ADMIN"),
        intent=ChatIntent.GENERAL_CHAT,
    )

    assert response.answer == "根据员工手册。"
    assert "未经过知识库检索" not in response.answer
    assert response.notice == "这条回答没有经过知识库检索，内容仅供参考，请结合实际情况判断。"
    assert response.answer_mode == "general_chat"
    assert response.knowledge_base_searched is False
    assert response.sources == []
    assert rag_service.prepare_calls == 0
    assert query_cache.get_calls == []
    assert query_cache.put_calls == []
    assert service.session_calls == [{"session_id": None, "kb_ids": [], "user": CurrentUser(user_id=1, department_id="engineering", role="ADMIN")}]
    assert service.saved_turns[0]["kb_ids"] is None
    assert service.saved_turns[0]["token_count"] == 4
    assert service.saved_turns[0]["answer_mode"] == "general_chat"
    assert service.saved_turns[0]["knowledge_base_searched"] is False


async def test_session_meta_uses_history_and_saves_turn() -> None:
    rag_service = FakeRagService(_prepared_context())
    service = InMemorySynchronousChatService(rag_service)

    response = await service.query(
        question="我刚才问了什么？",
        kb_ids=[],
        session_id="session-1",
        user=CurrentUser(user_id=1, department_id="engineering", role="ADMIN"),
        intent=ChatIntent.SESSION_META,
    )

    assert response.answer == "根据员工手册。"
    assert response.answer_mode == "session_meta"
    assert response.knowledge_base_searched is False
    assert rag_service.prepare_calls == 0
    assert service.session_calls[0]["session_id"] == "session-1"
    assert len(service.saved_turns) == 1
    assert service.saved_turns[0]["kb_ids"] is None
    assert service.saved_turns[0]["token_count"] == 4
    assert service.saved_turns[0]["answer_mode"] == "session_meta"
    assert service.saved_turns[0]["knowledge_base_searched"] is False
    assert service.query_cache.get_calls == []


async def test_mixed_returns_fixed_answer_and_saves_turn() -> None:
    rag_service = FakeRagService(_prepared_context())
    service = InMemorySynchronousChatService(rag_service)

    response = await service.query(
        question="查年假制度并写一首诗",
        kb_ids=[2],
        session_id=None,
        user=CurrentUser(user_id=1, department_id="engineering", role="ADMIN"),
        intent=ChatIntent.MIXED,
    )

    assert response.session_id == "session-1"
    assert response.answer == "你的问题同时包含知识库查询和通用问题，请拆分后分别提问。"
    assert response.answer_mode == "uncertain"
    assert rag_service.prepare_calls == 0
    assert service.session_calls[0]["session_id"] is None
    assert len(service.saved_turns) == 1
    assert service.saved_turns[0]["kb_ids"] is None
    assert service.saved_turns[0]["answer_mode"] == "uncertain"
    assert service.saved_turns[0]["knowledge_base_searched"] is False
    assert service.query_cache.get_calls == []
