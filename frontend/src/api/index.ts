import request from './request';
import type {
  ApiResponse,
  ChatMessageItem,
  ChatQueryResponse,
  ChatSessionItem,
  ChunkSummary,
  DocumentUploadResponse,
  EvalDataset,
  EvalDatasetWriteRequest,
  EvaluationReport,
  IndexStatusResponse,
  KnowledgeBase,
  KnowledgeBaseCreateRequest,
  KbDocument,
  LoginRequest,
  TokenStats,
} from '@/types';

export const authApi = {
  login: (data: LoginRequest) => request.post<ApiResponse<string>>('/auth/login', data),
  logout: () => request.post<ApiResponse<void>>('/auth/logout'),
};

export const kbApi = {
  create: (data: KnowledgeBaseCreateRequest) =>
    request.post<ApiResponse<KnowledgeBase>>('/kb', data),
  list: () => request.get<ApiResponse<KnowledgeBase[]>>('/kb'),
  uploadDocument: (kbId: number, file: File) => {
    const formData = new FormData();
    formData.append('file', file);
    return request.post<ApiResponse<DocumentUploadResponse>>(
      `/kb/${kbId}/documents`,
      formData,
      { skipGlobalErrorMessage: true },
    );
  },
  getDocuments: (kbId: number) =>
    request.get<ApiResponse<KbDocument[]>>(`/kb/${kbId}/documents`),
  getDocumentStatus: (kbId: number, docId: number) =>
    request.get<ApiResponse<IndexStatusResponse>>(`/kb/${kbId}/documents/${docId}/status`),
  deleteDocument: (kbId: number, docId: number) =>
    request.delete<ApiResponse<void>>(`/kb/${kbId}/documents/${docId}`, {
      skipGlobalErrorMessage: true,
    }),
  reindexDocument: (kbId: number, docId: number) =>
    request.post<ApiResponse<{ doc_id: number; task_id: number; message: string }>>(
      `/kb/${kbId}/documents/${docId}/reindex`,
      undefined,
      { skipGlobalErrorMessage: true },
    ),
  downloadDocument: (kbId: number, docId: number) =>
    request.get<Blob>(`/kb/${kbId}/documents/${docId}/download`, {
      responseType: 'blob',
    }),
};

export const chatApi = {
  syncQuery: (data: { question: string; kb_ids: number[]; session_id?: string | null }) =>
    request.post<ApiResponse<ChatQueryResponse>>('/chat', data),
  listSessions: () => request.get<ApiResponse<ChatSessionItem[]>>('/chat/sessions'),
  getMessages: (sessionId: string) =>
    request.get<ApiResponse<ChatMessageItem[]>>(`/chat/sessions/${sessionId}/messages`),
};

export const evalApi = {
  runEvaluation: (kbId: number, version: string) =>
    request.post<ApiResponse<EvaluationReport>>(`/eval/${kbId}/run`, null, {
      params: { version },
    }),
  getHistory: (kbId: number) =>
    request.get<ApiResponse<EvaluationReport[]>>(`/eval/${kbId}/history`),
  listDataset: (kbId: number) =>
    request.get<ApiResponse<EvalDataset[]>>(`/eval/${kbId}/dataset`),
  addQuestion: (kbId: number, data: EvalDatasetWriteRequest) =>
    request.post<ApiResponse<EvalDataset>>(`/eval/${kbId}/dataset`, data),
  updateQuestion: (kbId: number, id: number, data: EvalDatasetWriteRequest) =>
    request.put<ApiResponse<EvalDataset>>(`/eval/${kbId}/dataset/${id}`, data),
  deleteQuestion: (kbId: number, id: number) =>
    request.delete<ApiResponse<void>>(`/eval/${kbId}/dataset/${id}`),
  listChunks: (kbId: number) =>
    request.get<ApiResponse<ChunkSummary[]>>(`/eval/${kbId}/chunks`),
};

export const feedbackApi = {
  submit: (messageId: number, feedback: -1 | 1, comment?: string) =>
    request.post<ApiResponse<void>>(`/feedback/${messageId}`, {
      feedback,
      comment: comment || null,
    }),
  remove: (messageId: number) =>
    request.post<ApiResponse<void>>(`/feedback/${messageId}`, { feedback: null }),
};

export const statsApi = {
  getTokenStats: () => request.get<ApiResponse<TokenStats>>('/stats/tokens'),
};
