from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any

from app.core.context import CurrentUser
from app.schemas.query_cache import QueryCacheEntry
from app.schemas.rag import ChatIntent, SourceCitation
from app.services.rag_query_v4 import PreparedRagContext
from app.services.source_builder import FinalizedAnswer
from app.services.streaming_chat import SseEvent, StreamingChatService


class TimeoutStreamingChatService(StreamingChatService):
    async def _stream_success(
        self,
        *,
        question: str,
        kb_ids: list[int],
        session_id: str | None,
        user: CurrentUser,
        started_at: float,
    ) -> AsyncIterator[SseEvent]:
        await asyncio.sleep(0.01)
        if False:
            yield SseEvent(event="done", data="")


async def test_stream_timeout_returns_fixed_error_event() -> None:
    service = TimeoutStreamingChatService(
        session_factory=Any,
        rag_service_factory=lambda session: Any,
        query_cache=FakeQueryCache(),
        timeout_seconds=0.001,
    )

    events = [
        event
        async for event in service.stream(
            question="年假怎么申请？",
            kb_ids=[2],
            session_id=None,
            user=CurrentUser(user_id=1, department_id="engineering", role="ADMIN"),
        )
    ]

    assert events == [SseEvent(event="error", data='{"message":"生成超时，请稍后重试"}')]


class FakeSessionContext:
    async def __aenter__(self) -> object:
        return object()

    async def __aexit__(self, *args: object) -> None:
        return None


class FakeSessionFactory:
    def __call__(self) -> FakeSessionContext:
        return FakeSessionContext()


@dataclass
class FakeChunk:
    content: str
    usage_metadata: dict[str, int] | None = None

    def __add__(self, other: "FakeChunk") -> "FakeChunk":
        return FakeChunk(
            content=self.content + other.content,
            usage_metadata=other.usage_metadata or self.usage_metadata,
        )


class FakeTokenMetrics:
    def __init__(self) -> None:
        self.tokens: list[int] = []

    async def record_generation_tokens(self, *, tokens: int, source: str = "provider") -> None:
        self.tokens.append(tokens)


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


class FakeChatModel:
    def __init__(self, chunks: list[FakeChunk] | None = None, error: Exception | None = None) -> None:
        self.chunks = chunks if chunks is not None else [
            FakeChunk(content="根据"),
            FakeChunk(content="员工手册。", usage_metadata={"output_tokens": 4}),
        ]
        self.error = error

    async def astream(self, messages: list[object]) -> AsyncIterator[FakeChunk]:
        if self.error is not None:
            raise self.error
        for chunk in self.chunks:
            yield chunk


class FakeRagService:
    def __init__(
        self,
        prepared_context: PreparedRagContext | None,
        *,
        final_sources: list[SourceCitation] | None | object = ..., 
        final_answer: str | None = None,
        chat_model: FakeChatModel | None = None,
    ) -> None:
        self.prepared_context = prepared_context
        self.final_sources = (
            prepared_context.sources if final_sources is ... and prepared_context is not None else final_sources
        )
        self.final_answer = final_answer
        self.chat_model = chat_model or FakeChatModel()
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
        if not isinstance(self.final_sources, list):
            return None
        return FinalizedAnswer(
            answer=self.final_answer if self.final_answer is not None else str(kwargs["answer"]),
            sources=self.final_sources,
        )


class InMemoryStreamingChatService(StreamingChatService):
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


class FailingSaveStreamingChatService(InMemoryStreamingChatService):
    async def _save_turn(self, **kwargs: object) -> None:
        raise RuntimeError("message persistence failed")


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


def _cache_entry() -> QueryCacheEntry:
    return QueryCacheEntry(
        version=2,
        answer="缓存回答。[参考1]",
        sources=_prepared_context().sources,
        hit_count=1,
    )


async def test_stream_saves_complete_turn_and_injects_recent_history() -> None:
    rag_service = FakeRagService(_prepared_context())
    service = InMemoryStreamingChatService(rag_service)

    events = [
        event
        async for event in service.stream(
            question="年假怎么申请？",
            kb_ids=[2],
            session_id="session-1",
            user=CurrentUser(user_id=1, department_id="engineering", role="ADMIN"),
        )
    ]

    assert [event.event for event in events] == ["status", "status", "token", "token", "done"]
    assert rag_service.received_history == ["earlier-user", "earlier-assistant"]
    assert len(service.saved_turns) == 1
    saved_turn = service.saved_turns[0]
    assert saved_turn["session_id"] == "session-1"
    assert saved_turn["question"] == "年假怎么申请？"
    assert saved_turn["answer"] == "根据员工手册。"
    assert saved_turn["kb_ids"] == [2]
    assert saved_turn["sources"] == [
        {
            "reference_index": 1,
            "document_id": 1,
            "document_name": "员工手册.pdf",
            "kb_id": 2,
            "chunk_id": 10,
            "chunk_index": 0,
            "page_number": 12,
            "section_title": None,
            "excerpt": "年假规则",
            "score": 0.9,
        }
    ]
    assert saved_turn["token_count"] == 4
    assert isinstance(saved_turn["latency_ms"], int)
    assert saved_turn["latency_ms"] >= 0
    assert rag_service.token_metrics.tokens == [4]
    assert service.query_cache.get_calls == []
    assert service.query_cache.put_calls == []


async def test_stream_uses_normalized_answer_from_finalization_in_done_and_persistence() -> None:
    source = _prepared_context().sources[0].model_copy(update={"reference_index": 1})
    rag_service = FakeRagService(
        _prepared_context(),
        final_sources=[source],
        final_answer="根据员工手册（来源：[参考1]）。",
        chat_model=FakeChatModel(chunks=[FakeChunk(content="根据员工手册（来源：[参考4]）。")]),
    )
    service = InMemoryStreamingChatService(rag_service)

    events = [
        event
        async for event in service.stream(
            question="年假怎么申请？",
            kb_ids=[2],
            session_id="session-1",
            user=CurrentUser(user_id=1, department_id="engineering", role="ADMIN"),
        )
    ]

    assert events[-1].event == "done"
    assert '"answer":"根据员工手册（来源：[参考1]）。"' in events[-1].data
    assert service.saved_turns[0]["answer"] == "根据员工手册（来源：[参考1]）。"


async def test_stream_saves_refusal_turn_when_context_is_missing() -> None:
    service = InMemoryStreamingChatService(FakeRagService(None))

    events = [
        event
        async for event in service.stream(
            question="年假怎么申请？",
            kb_ids=[2],
            session_id=None,
            user=CurrentUser(user_id=1, department_id="engineering", role="ADMIN"),
        )
    ]

    assert [event.event for event in events] == ["status", "token", "done"]
    assert events[1].data == "在知识库中未找到与该问题相关的内容。"
    assert '"sources":[]' in events[2].data
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


async def test_stream_returns_error_without_saving_turn_when_model_returns_no_text() -> None:
    service = InMemoryStreamingChatService(
        FakeRagService(_prepared_context(), chat_model=FakeChatModel(chunks=[]))
    )

    events = [
        event
        async for event in service.stream(
            question="年假怎么申请？",
            kb_ids=[2],
            session_id=None,
            user=CurrentUser(user_id=1, department_id="engineering", role="ADMIN"),
        )
    ]

    assert [event.event for event in events] == ["status", "status", "error"]
    assert service.saved_turns == []


async def test_stream_saves_model_explicit_refusal() -> None:
    service = InMemoryStreamingChatService(
        FakeRagService(
            _prepared_context(),
            final_sources=None,
            chat_model=FakeChatModel(chunks=[FakeChunk(content="在知识库中未找到相关内容。")]),
        )
    )

    events = [
        event
        async for event in service.stream(
            question="年假怎么申请？",
            kb_ids=[2],
            session_id=None,
            user=CurrentUser(user_id=1, department_id="engineering", role="ADMIN"),
        )
    ]

    assert [event.event for event in events] == ["status", "status", "token", "done"]
    assert '"sources":[]' in events[-1].data
    assert len(service.saved_turns) == 1
    saved_turn = service.saved_turns[0]
    assert saved_turn["question"] == "年假怎么申请？"
    assert saved_turn["answer"] == "在知识库中未找到相关内容。"
    assert saved_turn["kb_ids"] == [2]
    assert saved_turn["sources"] == []
    assert saved_turn["answer_mode"] == "knowledge_base"
    assert saved_turn["knowledge_base_searched"] is True
    assert service.query_cache.put_calls == []


async def test_stream_returns_fixed_error_without_saving_turn_when_generation_fails() -> None:
    service = InMemoryStreamingChatService(
        FakeRagService(
            _prepared_context(),
            chat_model=FakeChatModel(error=RuntimeError("provider unavailable")),
        )
    )

    events = [
        event
        async for event in service.stream(
            question="年假怎么申请？",
            kb_ids=[2],
            session_id=None,
            user=CurrentUser(user_id=1, department_id="engineering", role="ADMIN"),
        )
    ]

    assert [event.event for event in events] == ["status", "status", "error"]
    assert events[-1].data == '{"message":"请求处理失败，请稍后重试"}'
    assert service.saved_turns == []


async def test_first_turn_cache_hit_emits_status_token_done_and_saves_zero_token_turn() -> None:
    rag_service = FakeRagService(_prepared_context())
    query_cache = FakeQueryCache(_cache_entry())
    service = InMemoryStreamingChatService(rag_service, query_cache)
    service.history = []

    events = [
        event
        async for event in service.stream(
            question="年假怎么申请？",
            kb_ids=[2],
            session_id=None,
            user=CurrentUser(user_id=1, department_id="engineering", role="ADMIN"),
        )
    ]

    assert [event.event for event in events] == ["status", "token", "done"]
    assert '"session_id":"session-1"' in events[0].data
    assert events[1].data == "缓存回答。[参考1]"
    assert '"sources":[{' in events[2].data
    assert '"latency_ms":' in events[2].data
    assert rag_service.prepare_calls == 0
    assert query_cache.get_calls == [("年假怎么申请？", [2])]
    assert query_cache.put_calls == []
    assert service.saved_turns[0]["token_count"] == 0


async def test_first_turn_cache_miss_saves_before_writing_cache() -> None:
    rag_service = FakeRagService(_prepared_context())
    query_cache = FakeQueryCache()
    service = InMemoryStreamingChatService(rag_service, query_cache)
    service.history = []

    events = [
        event
        async for event in service.stream(
            question="年假怎么申请？",
            kb_ids=[2],
            session_id=None,
            user=CurrentUser(user_id=1, department_id="engineering", role="ADMIN"),
        )
    ]

    assert events[-1].event == "done"
    assert rag_service.prepare_calls == 1
    assert query_cache.get_calls == [("年假怎么申请？", [2])]
    assert len(query_cache.put_calls) == 1
    assert query_cache.put_calls[0][2].answer == "根据员工手册。"


async def test_non_empty_history_skips_cache_read_and_write() -> None:
    rag_service = FakeRagService(_prepared_context())
    query_cache = FakeQueryCache(_cache_entry())
    service = InMemoryStreamingChatService(rag_service, query_cache)

    events = [
        event
        async for event in service.stream(
            question="年假怎么申请？",
            kb_ids=[2],
            session_id="session-1",
            user=CurrentUser(user_id=1, department_id="engineering", role="ADMIN"),
        )
    ]

    assert events[-1].event == "done"
    assert rag_service.received_history == ["earlier-user", "earlier-assistant"]
    assert query_cache.get_calls == []
    assert query_cache.put_calls == []


async def test_cache_hit_persistence_failure_emits_no_success_done() -> None:
    query_cache = FakeQueryCache(_cache_entry())
    service = FailingSaveStreamingChatService(FakeRagService(_prepared_context()), query_cache)
    service.history = []

    events = [
        event
        async for event in service.stream(
            question="年假怎么申请？",
            kb_ids=[2],
            session_id=None,
            user=CurrentUser(user_id=1, department_id="engineering", role="ADMIN"),
        )
    ]

    assert [event.event for event in events] == ["status", "error"]
    assert query_cache.put_calls == []


async def test_general_chat_stream_skips_retrieval_cache_and_saves_without_kb_scope() -> None:
    rag_service = FakeRagService(_prepared_context())
    query_cache = FakeQueryCache(_cache_entry())
    service = InMemoryStreamingChatService(rag_service, query_cache)

    events = [
        event
        async for event in service.stream(
            question="写一段欢迎词",
            kb_ids=[2],
            session_id=None,
            user=CurrentUser(user_id=1, department_id="engineering", role="ADMIN"),
            intent=ChatIntent.GENERAL_CHAT,
        )
    ]

    assert [event.event for event in events] == ["status", "token", "token", "done"]
    assert events[0].data == (
        '{"type":"GENERATING","message":"正在生成回答...","session_id":"session-1"}'
    )
    assert "这条回答没有经过知识库检索，内容仅供参考，请结合实际情况判断。" in events[-1].data
    assert all("这条回答没有经过知识库检索" not in event.data for event in events if event.event == "token")
    assert rag_service.prepare_calls == 0
    assert query_cache.get_calls == []
    assert query_cache.put_calls == []
    assert service.session_calls[0]["kb_ids"] == []
    assert service.saved_turns[0]["kb_ids"] is None
    assert service.saved_turns[0]["answer_mode"] == "general_chat"


async def test_session_meta_stream_uses_history_and_saves_turn() -> None:
    rag_service = FakeRagService(_prepared_context())
    service = InMemoryStreamingChatService(rag_service)

    events = [
        event
        async for event in service.stream(
            question="我刚才问了什么？",
            kb_ids=[],
            session_id="session-1",
            user=CurrentUser(user_id=1, department_id="engineering", role="ADMIN"),
            intent=ChatIntent.SESSION_META,
        )
    ]

    assert [event.event for event in events] == ["status", "token", "token", "done"]
    assert '"answer_mode":"session_meta"' in events[-1].data
    assert rag_service.prepare_calls == 0
    assert service.session_calls[0]["session_id"] == "session-1"
    assert len(service.saved_turns) == 1
    assert service.saved_turns[0]["kb_ids"] is None
    assert service.saved_turns[0]["answer_mode"] == "session_meta"
    assert service.saved_turns[0]["knowledge_base_searched"] is False
    assert service.query_cache.get_calls == []


async def test_mixed_stream_saves_fixed_answer_without_generation() -> None:
    rag_service = FakeRagService(_prepared_context())
    service = InMemoryStreamingChatService(rag_service)

    events = [
        event
        async for event in service.stream(
            question="查年假制度并写一首诗",
            kb_ids=[2],
            session_id=None,
            user=CurrentUser(user_id=1, department_id="engineering", role="ADMIN"),
            intent=ChatIntent.MIXED,
        )
    ]

    assert [event.event for event in events] == ["token", "done"]
    assert '"answer_mode":"uncertain"' in events[-1].data
    assert '"session_id":"session-1"' in events[-1].data
    assert rag_service.prepare_calls == 0
    assert service.session_calls[0]["session_id"] is None
    assert '"session_id":"session-1"' in events[-1].data
    assert len(service.saved_turns) == 1
    assert service.saved_turns[0]["kb_ids"] is None
    assert service.saved_turns[0]["answer_mode"] == "uncertain"
    assert service.saved_turns[0]["knowledge_base_searched"] is False
    assert service.query_cache.get_calls == []
