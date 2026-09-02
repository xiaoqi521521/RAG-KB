from __future__ import annotations

from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass
from datetime import datetime
from types import SimpleNamespace

from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from app.core.context import CurrentUser
from app.schemas.rag import ChatIntent, ChatQueryResponse
from app.services.intent_classifier import IntentDecision


def _user() -> CurrentUser:
    return CurrentUser(user_id=1, department_id="engineering", role="ADMIN")


class FakePermissionService:
    def __init__(self, forbidden_kb_id: int | None = None) -> None:
        self.forbidden_kb_id = forbidden_kb_id
        self.read_checks: list[int] = []

    async def require_read(self, kb_id: int, user: CurrentUser) -> None:
        self.read_checks.append(kb_id)
        if kb_id == self.forbidden_kb_id:
            raise HTTPException(status_code=403, detail="无权访问该知识库")


class FakeIntentClassifier:
    def __init__(self, intent: ChatIntent = ChatIntent.KNOWLEDGE_BASE_QUERY) -> None:
        self.intent = intent
        self.questions: list[str] = []

    async def classify_with_context(
        self,
        question: str,
        *,
        history: Sequence[object] = (),
        has_knowledge_base_history: bool = False,
    ) -> IntentDecision:
        self.questions.append(question)
        return IntentDecision(intent=self.intent)


@dataclass(frozen=True)
class FakeStreamEvent:
    event: str
    data: str


class FakeStreamingChatService:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    async def stream(
        self,
        *,
        question: str,
        kb_ids: list[int],
        session_id: str | None,
        user: CurrentUser,
        started_at: float | None = None,
        intent: ChatIntent = ChatIntent.KNOWLEDGE_BASE_QUERY,
        rewritten_question: str | None = None,
    ) -> AsyncIterator[FakeStreamEvent]:
        self.calls.append(
            {
                "question": question,
                "kb_ids": kb_ids,
                "session_id": session_id,
                "user_id": user.user_id,
                "started_at": started_at,
                "intent": intent,
                "rewritten_question": rewritten_question,
            }
        )
        yield FakeStreamEvent(
            event="status",
            data='{"type":"RETRIEVING","message":"正在检索知识库...","session_id":"new-session"}',
        )
        yield FakeStreamEvent(
            event="status",
            data='{"type":"GENERATING","message":"已找到相关内容，正在生成回答..."}',
        )
        yield FakeStreamEvent(event="token", data="根据")
        yield FakeStreamEvent(
            event="done",
            data=(
                '{"sources":[{"reference_index":1,"document_id":1,'
                '"document_name":"员工手册.pdf","kb_id":2,"chunk_id":10,'
                '"chunk_index":0,"page_number":12,"section_title":null,'
                '"excerpt":"年假规则","score":0.9}],"latency_ms":12}'
            ),
        )


class FakeSynchronousChatService:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    async def query(
        self,
        *,
        question: str,
        kb_ids: list[int],
        session_id: str | None,
        user: CurrentUser,
        started_at: float | None = None,
        intent: ChatIntent = ChatIntent.KNOWLEDGE_BASE_QUERY,
        rewritten_question: str | None = None,
    ) -> ChatQueryResponse:
        self.calls.append(
            {
                "question": question,
                "kb_ids": kb_ids,
                "session_id": session_id,
                "user_id": user.user_id,
                "started_at": started_at,
                "intent": intent,
                "rewritten_question": rewritten_question,
            }
        )
        return ChatQueryResponse(
            session_id="new-session",
            answer="需要通过钉钉提交申请。[参考1]",
            sources=[],
            hit_count=0,
            latency_ms=12,
        )


class FakeChatSessionService:
    def __init__(self) -> None:
        self.message_calls: list[tuple[str, int]] = []
        self.delete_calls: list[tuple[str, int]] = []
        self.delete_status_code: int | None = None
        self.messages: list[SimpleNamespace] = [
            SimpleNamespace(
                id=1,
                session_id="session-1",
                role="USER",
                content="年假怎么申请？",
                sources=None,
                token_count=0,
                latency_ms=0,
                feedback=None,
                created_at=datetime(2026, 1, 1),
            )
        ]

    async def list_sessions(self, user: CurrentUser) -> list[SimpleNamespace]:
        return [
            SimpleNamespace(
                id="session-1",
                user_id=user.user_id,
                kb_ids="[2]",
                title="年假怎么申请？",
                message_count=2,
                created_at=datetime(2026, 1, 1),
                last_active_at=datetime(2026, 1, 2),
                is_deleted=False,
            )
        ]

    async def list_messages(self, session_id: str, user: CurrentUser) -> list[SimpleNamespace]:
        self.message_calls.append((session_id, user.user_id))
        return self.messages

    async def delete_session(self, session_id: str, user: CurrentUser) -> None:
        self.delete_calls.append((session_id, user.user_id))
        if self.delete_status_code is not None:
            raise HTTPException(status_code=self.delete_status_code, detail="对话会话不存在")


def _client(
    permission_service: FakePermissionService,
    streaming_service: FakeStreamingChatService,
    chat_session_service: FakeChatSessionService | None = None,
    synchronous_service: FakeSynchronousChatService | None = None,
    intent_classifier: FakeIntentClassifier | None = None,
) -> TestClient:
    from app.api.routes import chat

    app = FastAPI()
    app.include_router(chat.router, prefix="/api/v1/chat")
    app.dependency_overrides[chat.get_permission_service] = lambda: permission_service
    app.dependency_overrides[chat.get_streaming_chat_service] = lambda: streaming_service
    if synchronous_service is not None:
        app.dependency_overrides[chat.get_synchronous_chat_service] = lambda: synchronous_service
    if chat_session_service is not None:
        app.dependency_overrides[chat.get_chat_session_service] = lambda: chat_session_service
    app.dependency_overrides[chat.get_intent_classifier] = lambda: intent_classifier or FakeIntentClassifier()
    app.dependency_overrides[chat.get_current_user] = lambda: _user()
    return TestClient(app)


def test_sync_endpoint_returns_complete_answer_and_session_id() -> None:
    permission_service = FakePermissionService()
    synchronous_service = FakeSynchronousChatService()

    with _client(
        permission_service,
        FakeStreamingChatService(),
        synchronous_service=synchronous_service,
    ) as client:
        response = client.post(
            "/api/v1/chat",
            json={"question": " 年假怎么申请？ ", "kb_ids": [2, 2]},
        )

    assert response.status_code == 200
    assert response.json()["data"]["session_id"] == "new-session"
    assert permission_service.read_checks == [2]
    assert len(synchronous_service.calls) == 1
    call = synchronous_service.calls[0]
    assert call["question"] == "年假怎么申请？"
    assert call["kb_ids"] == [2]
    assert call["session_id"] is None
    assert call["user_id"] == 1
    assert isinstance(call["started_at"], float)
    assert call["intent"] == ChatIntent.KNOWLEDGE_BASE_QUERY


def test_sync_endpoint_stops_before_session_creation_when_permission_denied() -> None:
    permission_service = FakePermissionService(forbidden_kb_id=3)
    synchronous_service = FakeSynchronousChatService()

    with _client(
        permission_service,
        FakeStreamingChatService(),
        synchronous_service=synchronous_service,
    ) as client:
        response = client.post(
            "/api/v1/chat",
            json={"question": "年假怎么申请？", "kb_ids": [2, 3]},
        )

    assert response.status_code == 403
    assert permission_service.read_checks == [2, 3]
    assert synchronous_service.calls == []


def test_stream_endpoint_returns_ordered_sse_events_for_first_question() -> None:
    permission_service = FakePermissionService()
    streaming_service = FakeStreamingChatService()

    with _client(permission_service, streaming_service) as client:
        response = client.get(
            "/api/v1/chat/stream",
            params=[("question", "年假怎么申请？"), ("kb_ids", "2")],
        )

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    assert response.text == (
        'event: status\ndata: {"type":"RETRIEVING","message":"正在检索知识库...",'
        '"session_id":"new-session"}\n\n'
        'event: status\ndata: {"type":"GENERATING","message":"已找到相关内容，正在生成回答..."}'
        "\n\n"
        "event: token\ndata: 根据\n\n"
        "event: done\ndata: "
        '{"sources":[{"reference_index":1,"document_id":1,"document_name":"员工手册.pdf",'
        '"kb_id":2,"chunk_id":10,"chunk_index":0,"page_number":12,"section_title":null,'
        '"excerpt":"年假规则","score":0.9}],"latency_ms":12}\n\n'
    )
    assert permission_service.read_checks == [2]
    assert len(streaming_service.calls) == 1
    call = streaming_service.calls[0]
    assert call["question"] == "年假怎么申请？"
    assert call["kb_ids"] == [2]
    assert call["session_id"] is None
    assert call["user_id"] == 1
    assert isinstance(call["started_at"], float)
    assert call["intent"] == ChatIntent.KNOWLEDGE_BASE_QUERY


def test_sync_general_chat_skips_kb_permissions_and_forwards_intent() -> None:
    permission_service = FakePermissionService(forbidden_kb_id=3)
    synchronous_service = FakeSynchronousChatService()
    classifier = FakeIntentClassifier(ChatIntent.GENERAL_CHAT)

    with _client(
        permission_service,
        FakeStreamingChatService(),
        synchronous_service=synchronous_service,
        intent_classifier=classifier,
    ) as client:
        response = client.post(
            "/api/v1/chat",
            json={"question": "写一段欢迎词", "kb_ids": [3]},
        )

    assert response.status_code == 200
    assert classifier.questions == ["写一段欢迎词"]
    assert permission_service.read_checks == []
    assert synchronous_service.calls[0]["intent"] == ChatIntent.GENERAL_CHAT


def test_stream_session_meta_without_session_id_is_handled_by_pipeline() -> None:
    streaming_service = FakeStreamingChatService()

    with _client(
        FakePermissionService(),
        streaming_service,
        intent_classifier=FakeIntentClassifier(ChatIntent.SESSION_META),
    ) as client:
        response = client.get("/api/v1/chat/stream", params={"question": "我刚才问了什么？"})

    assert response.status_code == 200
    assert streaming_service.calls[0]["intent"] == ChatIntent.SESSION_META


def test_stream_endpoint_checks_each_kb_before_starting_stream() -> None:
    permission_service = FakePermissionService(forbidden_kb_id=3)
    streaming_service = FakeStreamingChatService()

    with _client(permission_service, streaming_service) as client:
        response = client.get(
            "/api/v1/chat/stream",
            params=[("question", "年假怎么申请？"), ("kb_ids", "2"), ("kb_ids", "3")],
        )

    assert response.status_code == 403
    assert permission_service.read_checks == [2, 3]
    assert streaming_service.calls == []


def test_session_read_endpoints_return_existing_conversation_in_display_order() -> None:
    session_service = FakeChatSessionService()
    with _client(
        FakePermissionService(),
        FakeStreamingChatService(),
        session_service,
    ) as client:
        sessions = client.get("/api/v1/chat/sessions")
        messages = client.get("/api/v1/chat/sessions/session-1/messages")

    assert sessions.status_code == 200
    assert sessions.json()["data"][0]["id"] == "session-1"
    assert sessions.json()["data"][0]["message_count"] == 2
    assert messages.status_code == 200
    assert messages.json()["data"] == [
        {
            "id": 1,
            "session_id": "session-1",
            "role": "USER",
            "content": "年假怎么申请？",
            "sources": None,
            "token_count": 0,
            "latency_ms": 0,
            "feedback": None,
            "created_at": "2026-01-01T00:00:00",
        }
    ]
    assert session_service.message_calls == [("session-1", 1)]


def test_history_messages_keep_non_knowledge_base_answer_metadata() -> None:
    session_service = FakeChatSessionService()
    session_service.messages[0].answer_mode = "general_chat"
    session_service.messages[0].knowledge_base_searched = False
    with _client(
        FakePermissionService(),
        FakeStreamingChatService(),
        session_service,
    ) as client:
        response = client.get("/api/v1/chat/sessions/session-1/messages")

    assert response.status_code == 200
    assert response.json()["data"][0]["answer_mode"] == "general_chat"
    assert response.json()["data"][0]["knowledge_base_searched"] is False


def test_delete_session_deletes_the_current_users_history() -> None:
    session_service = FakeChatSessionService()
    with _client(
        FakePermissionService(),
        FakeStreamingChatService(),
        session_service,
    ) as client:
        response = client.delete("/api/v1/chat/sessions/session-1")

    assert response.status_code == 200
    assert response.json()["data"] is None
    assert session_service.delete_calls == [("session-1", 1)]


def test_delete_session_preserves_not_found_semantics() -> None:
    session_service = FakeChatSessionService()
    session_service.delete_status_code = 404
    with _client(
        FakePermissionService(),
        FakeStreamingChatService(),
        session_service,
    ) as client:
        response = client.delete("/api/v1/chat/sessions/another-users-session")

    assert response.status_code == 404
    assert session_service.delete_calls == [("another-users-session", 1)]
