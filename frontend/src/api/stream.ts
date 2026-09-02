import type { SourceCitation } from '@/types';

export type ChatStreamEvent =
  | { kind: 'status'; status: string; message: string; sessionId?: string }
  | { kind: 'token'; content: string }
  | { kind: 'done'; answer?: string; sources: SourceCitation[]; latencyMs: number; sessionId?: string; answerMode?: string; knowledgeBaseSearched?: boolean; notice?: string | null }
  | { kind: 'error'; message: string };

interface StreamChatParams {
  question: string;
  kbIds: number[];
  sessionId?: string | null;
  token: string;
  signal: AbortSignal;
  onEvent: (event: ChatStreamEvent) => void;
}

function parseSseBlock(block: string): { event: string; data: string } | null {
  let event = 'message';
  const dataLines: string[] = [];
  for (const line of block.split('\n')) {
    if (line.startsWith('event:')) {
      event = line.slice(6).trim();
    } else if (line.startsWith('data:')) {
      dataLines.push(line.slice(5).replace(/^ /, ''));
    }
  }
  if (dataLines.length === 0) {
    return null;
  }
  return { event, data: dataLines.join('\n') };
}

/** 携带 Bearer 头的 SSE 读取；EventSource 无法带认证头，因此用 fetch 解析。 */
export async function streamChat(params: StreamChatParams): Promise<void> {
  const search = new URLSearchParams();
  search.set('question', params.question);
  params.kbIds.forEach((id) => search.append('kb_ids', String(id)));
  if (params.sessionId) {
    search.set('session_id', params.sessionId);
  }

  const response = await fetch(`/api/v1/chat/stream?${search.toString()}`, {
    headers: {
      Authorization: `Bearer ${params.token}`,
      Accept: 'text/event-stream',
    },
    signal: params.signal,
  });

  if (!response.ok) {
    let errorMessage = `连接失败（HTTP ${response.status}）`;
    try {
      const body = await response.json();
      if (typeof body?.detail === 'string') {
        errorMessage = body.detail;
      }
    } catch {
      // 保留默认错误文案
    }
    throw new Error(errorMessage);
  }
  if (!response.body) {
    throw new Error('当前浏览器不支持流式读取');
  }

  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = '';

  const dispatch = (block: string) => {
    const parsed = parseSseBlock(block);
    if (!parsed) {
      return;
    }
    const { event, data } = parsed;
    if (event === 'token') {
      params.onEvent({ kind: 'token', content: data });
      return;
    }
    if (event === 'status') {
      try {
        const payload = JSON.parse(data);
          params.onEvent({
          kind: 'status',
          status: payload.type,
          message: payload.message,
          sessionId: payload.session_id,
        });
      } catch {
        // 忽略畸形 status 帧
      }
      return;
    }
    if (event === 'done') {
      try {
        const payload = JSON.parse(data);
        params.onEvent({
          kind: 'done',
          answer: typeof payload.answer === 'string' ? payload.answer : undefined,
          sources: Array.isArray(payload.sources) ? payload.sources : [],
          latencyMs: payload.latency_ms ?? 0,
          sessionId: typeof payload.session_id === 'string' ? payload.session_id : undefined,
          answerMode: payload.answer_mode,
          knowledgeBaseSearched: payload.knowledge_base_searched,
          notice: payload.notice,
        });
      } catch {
        params.onEvent({ kind: 'done', sources: [], latencyMs: 0 });
      }
      return;
    }
    if (event === 'error') {
      try {
        params.onEvent({ kind: 'error', message: JSON.parse(data).message ?? '生成失败' });
      } catch {
        params.onEvent({ kind: 'error', message: '生成失败' });
      }
    }
  };

  for (;;) {
    const { done, value } = await reader.read();
    if (done) {
      break;
    }
    buffer += decoder.decode(value, { stream: true });
    let separator = buffer.indexOf('\n\n');
    while (separator !== -1) {
      dispatch(buffer.slice(0, separator));
      buffer = buffer.slice(separator + 2);
      separator = buffer.indexOf('\n\n');
    }
  }
}
