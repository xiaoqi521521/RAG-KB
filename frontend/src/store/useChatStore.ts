import { create } from 'zustand';
import type { ChatMessage, SourceCitation } from '@/types';

interface ChatState {
  messages: ChatMessage[];
  sessionId: string | null;
  selectedKbIds: number[];
  isStreaming: boolean;
  streamStatus: string | null;
  panelMessageKey: string | null;
  panelHighlight: number | null;
  setSessionId: (id: string | null) => void;
  setSelectedKbIds: (ids: number[]) => void;
  setMessages: (messages: ChatMessage[]) => void;
  addMessage: (message: ChatMessage) => void;
  appendLastAssistant: (content: string) => void;
  setLastAssistantStatus: (status: string | null) => void;
  setLastAssistantDone: (
    sources: SourceCitation[],
    latencyMs: number,
    answer?: string,
    answerMode?: string,
    knowledgeBaseSearched?: boolean,
    notice?: string | null,
  ) => void;
  setLastAssistantMessageId: (messageId: number) => void;
  setStreaming: (streaming: boolean) => void;
  setStreamStatus: (status: string | null) => void;
  clearMessages: () => void;
  setMessageFeedback: (id: string, feedback: number | null) => void;
  openPanel: (messageKey: string, highlight?: number | null) => void;
  closePanel: () => void;
  setPanelHighlight: (index: number | null) => void;
  resetChat: () => void;
}

function updateLastAssistant(
  messages: ChatMessage[],
  patch: (message: ChatMessage) => ChatMessage,
): ChatMessage[] {
  const next = [...messages];
  for (let i = next.length - 1; i >= 0; i -= 1) {
    if (next[i].role === 'assistant') {
      next[i] = patch(next[i]);
      break;
    }
  }
  return next;
}

export const useChatStore = create<ChatState>((set) => ({
  messages: [],
  sessionId: null,
  selectedKbIds: [],
  isStreaming: false,
  streamStatus: null,
  panelMessageKey: null,
  panelHighlight: null,

  setSessionId: (id) => set({ sessionId: id }),
  setSelectedKbIds: (ids) => set({ selectedKbIds: ids }),
  setMessages: (messages) => set({ messages }),
  addMessage: (message) => set((s) => ({ messages: [...s.messages, message] })),

  appendLastAssistant: (content) =>
    set((s) => ({
      messages: updateLastAssistant(s.messages, (m) => ({ ...m, content: m.content + content })),
    })),

  setLastAssistantStatus: (status) =>
    set((s) => ({
      messages: updateLastAssistant(s.messages, (m) => ({ ...m, status })),
    })),

  setLastAssistantDone: (sources, latencyMs, answer, answerMode, knowledgeBaseSearched, notice) =>
    set((s) => ({
      messages: updateLastAssistant(s.messages, (m) => ({
        ...m,
        ...(answer !== undefined ? { content: answer } : {}),
        ...(answerMode !== undefined ? { answerMode } : {}),
        ...(knowledgeBaseSearched !== undefined ? { knowledgeBaseSearched } : {}),
        ...(notice !== undefined ? { notice } : {}),
        sources,
        latencyMs,
        streaming: false,
        status: null,
      })),
    })),

  setLastAssistantMessageId: (messageId) =>
    set((s) => ({
      messages: updateLastAssistant(s.messages, (m) => ({ ...m, messageId })),
    })),

  setStreaming: (streaming) => set({ isStreaming: streaming }),
  setStreamStatus: (status) => set({ streamStatus: status }),

  clearMessages: () =>
    set({ messages: [], sessionId: null, panelMessageKey: null, panelHighlight: null }),

  setMessageFeedback: (id, feedback) =>
    set((s) => ({
      messages: s.messages.map((m) => (m.id === id ? { ...m, feedback } : m)),
    })),

  openPanel: (messageKey, highlight) =>
    set({ panelMessageKey: messageKey, panelHighlight: highlight ?? null }),

  closePanel: () => set({ panelMessageKey: null, panelHighlight: null }),
  setPanelHighlight: (index) => set({ panelHighlight: index }),
  resetChat: () =>
    set({
      messages: [],
      sessionId: null,
      selectedKbIds: [],
      isStreaming: false,
      streamStatus: null,
      panelMessageKey: null,
      panelHighlight: null,
    }),
}));
