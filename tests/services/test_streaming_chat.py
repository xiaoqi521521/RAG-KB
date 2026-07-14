from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any

from app.core.context import CurrentUser
from app.schemas.rag import SourceCitation
from app.services.rag_query_v4 import PreparedRagContext
from app.services.streaming_chat import SseEvent, StreamingChatService


class TimeoutStreamingChatService(StreamingChatService):
    async def _stream_success(
        self,
        *,
        question: str,
        kb_ids: list[int],
        session_id: str | None,
        user: CurrentUser,
    ) -> AsyncIterator[SseEvent]:
        await asyncio.sleep(0.01)
        if False:
            yield SseEvent(event="done", data="")


async def test_stream_timeout_returns_fixed_error_event() -> None:
    service = TimeoutStreamingChatService(
        session_factory=Any,
        rag_service_factory=lambda session: Any,
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
        chat_model: FakeChatModel | None = None,
    ) -> None:
        self.prepared_context = prepared_context
        self.final_sources = (
            prepared_context.sources if final_sources is ... and prepared_context is not None else final_sources
        )
        self.chat_model = chat_model or FakeChatModel()
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
        return self.final_sources if isinstance(self.final_sources, list) else None


class InMemoryStreamingChatService(StreamingChatService):
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


async def test_stream_returns_refusal_without_saving_turn_when_context_is_missing() -> None:
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
    assert service.saved_turns == []


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


async def test_stream_does_not_save_model_explicit_refusal() -> None:
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
    assert service.saved_turns == []


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
