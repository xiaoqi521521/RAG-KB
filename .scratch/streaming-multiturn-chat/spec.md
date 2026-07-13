# 流式输出与多轮对话

Status: ready-for-agent

## Problem Statement

企业知识库问答当前只能等待完整回答后一次性返回，用户在检索和生成期间看不到进度，体感延迟较高。虽然数据模型已具备对话会话和消息表，现有查询链路没有读取或保存对话历史，用户无法在同一对话会话中连续追问，也无法从服务端恢复历史会话和引用来源。

## Solution

在既有 V4 RAG 查询能力上增加 SSE 流式问答和对话会话管理。流式端点先反馈检索状态，再逐片段返回模型回答，最后返回引用来源和总耗时。回答模型将本次 RAG 参考内容、当前问题之前最近五轮对话历史和当前问题组合为输入；检索仍只依据当前问题和当前请求的知识库范围执行。服务端在完整生成成功后保存本轮消息，提供会话和消息读取接口恢复历史。

## User Stories

1. As a 知识库用户, I want to receive a `RETRIEVING` status immediately after submitting a question, so that I know the system is working before an answer appears.
2. As a 知识库用户, I want to see an answer arrive as `token` events, so that I can start reading without waiting for the entire generation to finish.
3. As a 知识库用户, I want the final event to contain the answer's 引用来源 and latency, so that I can verify the answer after streaming completes.
4. As a 知识库用户, I want the first stream response to return a newly created `session_id` when I did not send one, so that I can continue the same 对话会话.
5. As a 知识库用户, I want a follow-up question in the same 对话会话 to use the recent conversation, so that I do not need to repeat prior context.
6. As a 知识库用户, I want the latest five rounds of 对话历史 to be considered in generation, so that follow-up context is useful without unbounded prompt growth.
7. As a 知识库用户, I want my current question to remain the final user message sent to the model, so that the model clearly answers the question I just asked.
8. As a 知识库用户, I want RAG retrieval for a follow-up to use my current question rather than a hidden history rewrite, so that retrieval behavior remains aligned with the reference design.
9. As a 知识库用户, I want to select `kb_ids` for each request, so that my current knowledge base range governs each answer even when I reuse a 对话会话.
10. As a 知识库用户, I want a clear fixed refusal returned as a normal stream completion when no 参考内容 is available, so that I can distinguish unavailable knowledge from a system error.
11. As a 知识库用户, I want the refusal completion to include empty 引用来源 and latency, so that the final SSE payload is structurally consistent.
12. As a 知识库用户, I want a clear fixed error event if generation, retrieval, or the stream timeout fails, so that I can stop waiting without seeing internal service details.
13. As a 知识库用户, I want to reopen my recent 对话会话 list in most-recently-active order, so that I can find and resume a prior conversation.
14. As a 知识库用户, I want to retrieve a 对话会话's messages in chronological order, including an assistant message's 引用来源, so that the conversation can be rendered faithfully.
15. As a knowledge base operator, I want successful assistant messages to retain generation token count and latency, so that cost and performance can be observed alongside existing token metrics.
16. As a knowledge base operator, I want aborted, timed-out, and failed streams not to create partial message records, so that later 对话历史 is not corrupted by incomplete answers.
17. As a frontend developer, I want the new stream endpoint's query parameter and citation fields to use existing snake_case conventions, so that it is consistent with current APIs.
18. As a frontend developer, I want the existing synchronous RAG query endpoint to remain unchanged, so that current consumers and debugging workflows continue to work.

## Implementation Decisions

- Add `GET /api/v1/chat/stream`. It accepts `question`, repeated `kb_ids`, and optional `session_id` as query parameters. The existing synchronous `POST /api/v1/rag/query` remains unchanged.
- The stream event contract is fixed:
  - First event is `status` with `type=RETRIEVING`, a user-facing message, and the active `session_id`.
  - After usable 参考内容 exists, emit `status` with `type=GENERATING`.
  - Each generated text fragment is a raw-text `token` event rather than JSON.
  - Normal completion emits `done` with snake_case `sources` using the existing `SourceCitation` shape and `latency_ms`.
  - Failures emit `error` with a fixed user-safe `message`; underlying exception detail is logged only.
- The connection timeout defaults to 60 seconds and is configurable through environment-backed settings. Timeout emits an `error` event, closes the stream, and does not save the turn.
- The streaming path evolves the V4 RAG pipeline rather than creating a parallel retrieval implementation. It preserves existing hard read-permission checks, hybrid retrieval, Reranker degradation, confidence handling, context trimming, citation resolution, faithfulness observation, and token metrics semantics.
- On every request, validate read access to each requested `kb_id` before retrieval. The current request's knowledge base range is used for retrieval; a 对话会话's stored range is not a lock on later requests.
- A new 对话会话 uses a UUID `session_id`, records the current user ID and initial knowledge base range, and starts with zero messages. An existing `session_id` is reused and its active timestamp is refreshed.
- Persist complete message history. For generation, load only the current question's preceding five rounds, at most ten USER and ASSISTANT messages, in chronological order. Provide the model with the system message containing the current 参考内容, then 对话历史, then the current user question.
- Do not apply 对话历史 to retrieval query rewriting in this phase; retrieval receives only the current `question`.
- On successful completion, persist one USER message and one ASSISTANT message. The assistant message contains the complete generated content, 引用来源, latency, and provider generation token count; store `0` when provider usage is unavailable.
- Create the 对话会话 title only after its first successful turn, using the first 50 characters of the first question. Every successful turn increments `message_count` by two and refreshes the active timestamp.
- Treat no usable 参考内容 as a successful refusal path: after `RETRIEVING`, send the fixed refusal as a `token`, followed by `done` with empty `sources` and actual `latency_ms`; do not emit `GENERATING`.
- Add session list and message history reads: list non-deleted sessions by descending active timestamp, and list messages within a session by ascending creation timestamp.
- Confirm the installed FastAPI streaming and LangChain model streaming APIs before coding, particularly cancellation propagation and final provider usage metadata.

## Testing Decisions

- The primary seam is the externally observable HTTP/SSE contract. Tests should consume the stream and assert event name, order, payload shape, completion behavior, and persisted results rather than asserting internal helper calls.
- Follow the existing RAG API testing style: construct a FastAPI test app and override dependencies with fakes at the route boundary. This keeps tests focused on permission prechecks and endpoint behavior.
- Add service-level behavior tests only where necessary to validate the conversation boundary with faked V4 retrieval/generation and persistence collaborators. Do not mock private implementation details.
- Verify first-turn session creation and the `session_id` in the initial status event.
- Verify reuse of an existing session, complete database history retention, and injection of only the chronological latest ten historical messages into generation.
- Verify that follow-up retrieval receives the current question, not a history-rewritten question.
- Verify the normal sequence `RETRIEVING -> GENERATING -> token+ -> done`, raw token data, snake_case sources, and actual latency in `done`.
- Verify the no-reference sequence `RETRIEVING -> token -> done`, fixed refusal, empty sources, and absence of `GENERATING`.
- Verify fixed `error` events for retrieval/generation failure and 60-second timeout, with no partial USER or ASSISTANT records.
- Verify successful completion writes the user and assistant pair, first-turn title, message count, sources, latency, and available-or-zero token count.
- Verify session listing order and message listing chronology.
- Preserve existing synchronous RAG API tests to demonstrate that the new stream path does not alter the existing JSON endpoint.

## Out of Scope

- Session ownership validation, tenant isolation, and prevention of cross-knowledge-base history use. These are explicitly deferred to the next permission phase.
- Rewriting retrieval queries from conversation history, HyDE changes, or new retrieval strategies.
- WebSocket transport, streaming resume after disconnect, explicit client cancellation APIs, and message-level regeneration.
- Frontend pages, browser-side EventSource integration, and UI rendering.
- Session deletion, rename, feedback workflows, and additional conversation management APIs.
- Changing the existing synchronous RAG endpoint's request or response contract.

## Further Notes

- This specification aligns with the reference chapter's two-stage RAG streaming model: retrieval is completed before text generation begins, while sources are returned only at normal completion.
- Existing models already represent sessions, messages, sources, token count, latency, title, active timestamp, and deletion status. The work should reuse them rather than introduce duplicate persistence models.
- The absence of session ownership and tenant checks is a deliberate temporary scope boundary requested during design, not an acceptable long-term security posture. The subsequent permissions phase must close it.
- The detailed design record remains available in the project documentation; this Spec is the tracker-facing source of implementation scope and acceptance behavior.
