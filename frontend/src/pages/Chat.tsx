import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { App, Button, Select, Tooltip } from 'antd';
import {
  ArrowUpOutlined,
  DislikeFilled,
  DislikeOutlined,
  LikeFilled,
  LikeOutlined,
  PlusOutlined,
  SendOutlined,
} from '@ant-design/icons';
import dayjs from 'dayjs';
import { chatApi, feedbackApi, kbApi } from '@/api';
import { streamChat } from '@/api/stream';
import CitationText from '@/components/CitationText';
import PageHeader from '@/components/PageHeader';
import SourceArchiveCard from '@/components/SourceArchiveCard';
import { useAuthStore } from '@/store/useAuthStore';
import { useChatStore } from '@/store/useChatStore';
import type {
  ChatMessage,
  ChatMessageItem,
  ChatSessionItem,
  KnowledgeBase,
  SourceCitation,
} from '@/types';

const NOT_FOUND_ANSWER = '在知识库中未找到与该问题相关的内容。';

function normalizeSources(value: ChatMessageItem['sources']): SourceCitation[] {
  if (Array.isArray(value)) {
    return value;
  }
  if (value && typeof value === 'object') {
    return Object.values(value);
  }
  return [];
}

function mapHistoryMessages(items: ChatMessageItem[]): ChatMessage[] {
  return items.map((item) => ({
    id: `history-${item.id}`,
    messageId: item.id,
    role: item.role === 'USER' ? 'user' : 'assistant',
    content: item.content,
    sources: item.role === 'ASSISTANT' ? normalizeSources(item.sources) : null,
    latencyMs: item.latency_ms,
    feedback: item.feedback,
    streaming: false,
    status: null,
    timestamp: new Date(item.created_at).getTime(),
  }));
}

function parseKbIds(value: string): number[] {
  try {
    const parsed = JSON.parse(value);
    return Array.isArray(parsed) ? parsed : [];
  } catch {
    return [];
  }
}

export default function ChatPage() {
  const { message } = App.useApp();
  const token = useAuthStore((s) => s.token);
  const {
    messages,
    sessionId,
    selectedKbIds,
    isStreaming,
    streamStatus,
    panelMessageKey,
    panelHighlight,
    setSessionId,
    setSelectedKbIds,
    setMessages,
    addMessage,
    appendLastAssistant,
    setLastAssistantStatus,
    setLastAssistantDone,
    setLastAssistantMessageId,
    setStreaming,
    setStreamStatus,
    clearMessages,
    setMessageFeedback,
    openPanel,
    closePanel,
    setPanelHighlight,
  } = useChatStore();

  const [knowledgeBases, setKnowledgeBases] = useState<KnowledgeBase[]>([]);
  const [sessions, setSessions] = useState<ChatSessionItem[]>([]);
  const [sessionsLoading, setSessionsLoading] = useState(false);
  const [inputValue, setInputValue] = useState('');
  const abortRef = useRef<AbortController | null>(null);
  const bottomRef = useRef<HTMLDivElement | null>(null);

  const selectedPanelMessage = useMemo(
    () => messages.find((item) => item.id === panelMessageKey) || null,
    [messages, panelMessageKey],
  );

  const fetchSessions = useCallback(async () => {
    setSessionsLoading(true);
    try {
      const res = await chatApi.listSessions();
      setSessions(res.data.data);
    } catch {
      message.error('读取历史会话失败');
    } finally {
      setSessionsLoading(false);
    }
  }, [message]);

  useEffect(() => {
    kbApi
      .list()
      .then((res) => {
        setKnowledgeBases(res.data.data);
        if (res.data.data.length > 0) {
          setSelectedKbIds([res.data.data[0].id]);
        }
      })
      .catch(() => message.error('读取知识库失败'));
    void fetchSessions();
  }, [fetchSessions, message, setSelectedKbIds]);

  useEffect(() => {
    bottomRef.current?.scrollIntoView({ block: 'end' });
  }, [messages, streamStatus]);

  const loadSession = useCallback(
    async (targetSessionId: string) => {
      try {
        const [session] = sessions.filter((item) => item.id === targetSessionId);
        const res = await chatApi.getMessages(targetSessionId);
        setMessages(mapHistoryMessages(res.data.data));
        setSessionId(targetSessionId);
        if (session) {
          setSelectedKbIds(parseKbIds(session.kb_ids));
        }
        const lastAssistant = [...res.data.data]
          .reverse()
          .find((item) => item.role === 'ASSISTANT' && item.sources);
        if (lastAssistant) {
          openPanel(`history-${lastAssistant.id}`);
        } else {
          closePanel();
        }
      } catch {
        message.error('读取会话失败');
      }
    },
    [closePanel, message, openPanel, sessions, setMessages, setSelectedKbIds, setSessionId],
  );

  const syncLastMessageId = useCallback(
    async (targetSessionId: string, answer: string) => {
      try {
        const res = await chatApi.getMessages(targetSessionId);
        const matched = [...res.data.data]
          .reverse()
          .find((item) => item.role === 'ASSISTANT' && item.content === answer);
        if (matched) {
          setLastAssistantMessageId(matched.id);
        }
      } catch {
        // 消息已成功保存；同步 ID 失败只影响本轮反馈按钮
      }
    },
    [setLastAssistantMessageId],
  );

  const handleSend = useCallback(async () => {
    const question = inputValue.trim();
    if (!question || isStreaming) {
      return;
    }
    if (selectedKbIds.length === 0) {
      message.warning('请选择至少一个知识库');
      return;
    }

    const now = Date.now();
    addMessage({
      id: `user-${now}`,
      messageId: null,
      role: 'user',
      content: question,
      sources: null,
      latencyMs: null,
      feedback: null,
      streaming: false,
      status: null,
      timestamp: now,
    });
    addMessage({
      id: `assistant-${now}`,
      messageId: null,
      role: 'assistant',
      content: '',
      sources: null,
      latencyMs: null,
      feedback: null,
      streaming: true,
      status: '正在建立连接',
      timestamp: now,
    });
    setInputValue('');
    setStreaming(true);
    setStreamStatus('正在建立连接');
    closePanel();

    const controller = new AbortController();
    abortRef.current = controller;
    let finalAnswer = '';
    let finishedSessionId = sessionId;

    try {
      await streamChat({
        question,
        kbIds: selectedKbIds,
        sessionId,
        token: token || '',
        signal: controller.signal,
        onEvent: (event) => {
          if (event.kind === 'status') {
            setLastAssistantStatus(event.message);
            setStreamStatus(event.message);
            if (event.sessionId) {
              finishedSessionId = event.sessionId;
              setSessionId(event.sessionId);
            }
          } else if (event.kind === 'token') {
            finalAnswer += event.content;
            appendLastAssistant(event.content);
          } else if (event.kind === 'done') {
            setLastAssistantDone(event.sources, event.latencyMs);
            if (event.sources.length > 0) {
              openPanel(`assistant-${now}`, event.sources[0]?.reference_index);
            }
          } else if (event.kind === 'error') {
            appendLastAssistant(finalAnswer ? `\n\n${event.message}` : event.message);
            setLastAssistantDone([], 0);
          }
        },
      });
    } catch (error) {
      if ((error as Error).name !== 'AbortError') {
        appendLastAssistant(finalAnswer ? '\n\n连接异常，请稍后重试。' : '连接异常，请稍后重试。');
        setLastAssistantDone([], 0);
      }
    } finally {
      abortRef.current = null;
      setStreaming(false);
      setStreamStatus(null);
      if (finishedSessionId && finalAnswer) {
        void syncLastMessageId(finishedSessionId, finalAnswer);
      }
      void fetchSessions();
    }
  }, [
    addMessage,
    appendLastAssistant,
    closePanel,
    fetchSessions,
    inputValue,
    isStreaming,
    message,
    openPanel,
    selectedKbIds,
    sessionId,
    setLastAssistantDone,
    setLastAssistantStatus,
    setSessionId,
    setStreaming,
    setStreamStatus,
    syncLastMessageId,
    token,
  ]);

  const handleFeedback = useCallback(
    async (chatMessage: ChatMessage, feedback: 1 | -1) => {
      const messageId = chatMessage.messageId;
      if (typeof messageId !== 'number') {
        return;
      }
      setMessageFeedback(chatMessage.id, feedback);
      try {
        await feedbackApi.submit(messageId, feedback);
        message.success(feedback === 1 ? '已记录“有用”' : '已记录“待改进”');
      } catch {
        setMessageFeedback(chatMessage.id, chatMessage.feedback);
        message.error('提交反馈失败');
      }
    },
    [message, setMessageFeedback],
  );

  const knowledgeBaseOptions = knowledgeBases.map((kb) => ({
    label: `${kb.name} · ${kb.permission}`,
    value: kb.id,
  }));

  return (
    <div className="h-full min-h-0 flex flex-col px-4 pt-4 md:px-6 md:pt-6">
      <PageHeader
        eyebrow="ONLINE RETRIEVAL"
        title="检索问答"
        description="回答只基于已授权知识库，每个编号引用都可回溯来源。"
        actions={
          <Button icon={<PlusOutlined />} onClick={clearMessages}>
            新建会话
          </Button>
        }
      />

      <div
        className={`flex-1 min-h-0 grid gap-4 pb-4 md:pb-6 ${
          panelMessageKey ? 'xl:grid-cols-[220px_minmax(0,1fr)_330px]' : 'xl:grid-cols-[220px_minmax(0,1fr)]'
        }`}
      >
        <aside className="hidden xl:flex archive-card flex-col overflow-hidden">
          <div className="px-3.5 py-3 border-b border-line">
            <div className="eyebrow">检索范围</div>
            <Select
              mode="multiple"
              value={selectedKbIds}
              options={knowledgeBaseOptions}
              onChange={setSelectedKbIds}
              placeholder="选择知识库"
              className="w-full mt-2"
              maxTagCount={2}
              size="small"
            />
          </div>
          <div className="px-3.5 py-3 border-b border-line flex items-center justify-between">
            <span className="text-[13px] font-medium">历史会话</span>
            <span className="font-data text-[11px] text-faint">{sessions.length}</span>
          </div>
          <div className="flex-1 overflow-auto p-2 space-y-1.5">
            {sessionsLoading && <div className="text-soft text-xs px-2 py-3">读取中...</div>}
            {!sessionsLoading && sessions.length === 0 && (
              <div className="text-faint text-xs px-2 py-3 leading-relaxed">
                暂无历史会话。发送第一个问题后，这里会保留会话线索。
              </div>
            )}
            {sessions.map((session) => (
              <button
                key={session.id}
                type="button"
                onClick={() => loadSession(session.id)}
                className={`w-full text-left px-2.5 py-2 rounded-[2px] border transition-colors ${
                  sessionId === session.id
                    ? 'border-pine bg-pine-wash'
                    : 'border-transparent hover:bg-[#f0f2ec]'
                }`}
              >
                <div className="text-[12.5px] line-clamp-2">
                  {session.title || '未命名会话'}
                </div>
                <div className="font-data text-[10.5px] text-faint mt-1">
                  {dayjs(session.last_active_at).format('MM-DD HH:mm')}
                </div>
              </button>
            ))}
          </div>
        </aside>

        <section className="min-h-0 archive-card flex flex-col overflow-hidden">
          <div className="xl:hidden px-4 pt-4">
            <Select
              mode="multiple"
              value={selectedKbIds}
              options={knowledgeBaseOptions}
              onChange={setSelectedKbIds}
              placeholder="选择知识库"
              className="w-full"
              maxTagCount={2}
            />
          </div>

          <div className="flex-1 min-h-[320px] overflow-auto px-4 md:px-6 py-5 space-y-6">
            {messages.length === 0 && (
              <div className="h-full min-h-[280px] flex flex-col items-center justify-center text-center px-6">
                <div className="refusal-stamp mb-5">RAG</div>
                <h2 className="text-lg font-semibold">向企业知识库提问</h2>
                <p className="text-soft text-[13px] mt-2 max-w-[420px] leading-relaxed">
                  选择一个或多个知识库后输入问题。系统会先检索、再精排、最后生成；未检出内容时将明确拒答。
                </p>
              </div>
            )}

            {messages.map((chatMessage) => (
              <article
                key={chatMessage.id}
                className={chatMessage.role === 'user' ? 'flex justify-end' : 'flex'}
              >
                {chatMessage.role === 'user' ? (
                  <div className="max-w-[78%] bg-pine-wash border border-[#d4e0da] px-4 py-3 rounded-[2px] text-[14px] whitespace-pre-wrap">
                    {chatMessage.content}
                  </div>
                ) : (
                  <div className="w-full max-w-[820px]">
                    <div className="flex items-center gap-2 mb-2">
                      <span className="eyebrow">ANSWER</span>
                      {chatMessage.latencyMs != null && (
                        <span className="font-data text-[11px] text-faint">
                          {chatMessage.latencyMs}ms
                        </span>
                      )}
                    </div>

                    {chatMessage.content === NOT_FOUND_ANSWER ? (
                      <div className="flex items-start gap-4">
                        <span className="refusal-stamp shrink-0">未检出</span>
                        <p className="text-soft text-[14px] leading-relaxed pt-1">
                          {chatMessage.content}
                        </p>
                      </div>
                    ) : chatMessage.content ? (
                      <CitationText
                        content={chatMessage.content}
                        highlight={panelMessageKey === chatMessage.id ? panelHighlight : null}
                        onCite={(index) =>
                          openPanel(chatMessage.id, index === panelHighlight ? null : index)
                        }
                      />
                    ) : null}

                    {chatMessage.streaming && (
                      <div className="retrieval-line mt-3">
                        <span className="retrieval-dot" />
                        {streamStatus || chatMessage.status}
                        <span className="stream-caret" />
                      </div>
                    )}

                    {chatMessage.sources && chatMessage.sources.length > 0 && (
                      <button
                        type="button"
                        onClick={() => openPanel(chatMessage.id)}
                        className="mt-3 text-[12.5px] text-pine hover:underline"
                      >
                        查看检索档案 · {chatMessage.sources.length} 条来源
                      </button>
                    )}

                    {!chatMessage.streaming && chatMessage.messageId != null && (
                      <div className="flex items-center gap-1 mt-3">
                        <Tooltip title="有用">
                          <Button
                            type="text"
                            size="small"
                            icon={
                              chatMessage.feedback === 1 ? <LikeFilled /> : <LikeOutlined />
                            }
                            onClick={() => handleFeedback(chatMessage, 1)}
                          />
                        </Tooltip>
                        <Tooltip title="待改进">
                          <Button
                            type="text"
                            size="small"
                            icon={
                              chatMessage.feedback === -1 ? (
                              <DislikeFilled />
                              ) : (
                                <DislikeOutlined />
                              )
                            }
                            onClick={() => handleFeedback(chatMessage, -1)}
                          />
                        </Tooltip>
                      </div>
                    )}
                  </div>
                )}
              </article>
            ))}
            <div ref={bottomRef} />
          </div>

          <div className="border-t border-line p-3 md:p-4 bg-[#f7f8f4]">
            <div className="flex items-end gap-2">
              <textarea
                value={inputValue}
                onChange={(event) => setInputValue(event.target.value)}
                onKeyDown={(event) => {
                  if (event.key === 'Enter' && !event.shiftKey) {
                    event.preventDefault();
                    void handleSend();
                  }
                }}
                rows={2}
                placeholder="输入问题，Enter 发送，Shift+Enter 换行"
                className="flex-1 resize-none border border-line rounded-[2px] bg-card px-3.5 py-2.5 text-[14px] outline-none focus:border-pine"
              />
              <Button
                type="primary"
                size="large"
                icon={isStreaming ? <ArrowUpOutlined /> : <SendOutlined />}
                disabled={isStreaming ? false : !inputValue.trim()}
                onClick={() => {
                  if (isStreaming) {
                    abortRef.current?.abort();
                  } else {
                    void handleSend();
                  }
                }}
              >
                {isStreaming ? '停止' : '发送'}
              </Button>
            </div>
            <div className="flex items-center justify-between mt-2 text-[11.5px] text-faint">
              <span>来源编号会对应右侧检索档案</span>
              <span className="font-data">{inputValue.length}/2000</span>
            </div>
          </div>
        </section>

        {panelMessageKey && (
          <aside className="archive-card flex min-h-[280px] flex-col overflow-hidden">
            <div className="px-4 py-3 border-b border-line flex items-start justify-between gap-2">
              <div>
                <div className="eyebrow">SOURCE ARCHIVE</div>
                <div className="text-[15px] font-semibold mt-1">检索档案</div>
              </div>
              <Button type="text" size="small" onClick={closePanel}>
                收起
              </Button>
            </div>
            <div className="flex-1 overflow-auto p-3 space-y-2.5">
              {selectedPanelMessage?.sources?.length ? (
                selectedPanelMessage.sources.map((source) => (
                  <SourceArchiveCard
                    key={source.chunk_id}
                    source={source}
                    active={panelHighlight === source.reference_index}
                    onClick={() =>
                      setPanelHighlight(
                        panelHighlight === source.reference_index
                          ? null
                          : source.reference_index,
                      )
                    }
                  />
                ))
              ) : (
                <div className="text-soft text-[13px] leading-relaxed p-2">
                  本次未检出可用来源。系统不会使用通用知识补全企业政策。
                </div>
              )}
            </div>
          </aside>
        )}
      </div>
    </div>
  );
}
