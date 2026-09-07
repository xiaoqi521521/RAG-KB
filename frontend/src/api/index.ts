import type { AxiosRequestConfig } from 'axios';
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
  EvaluationHistoryPage,
  IndexStatusResponse,
  KnowledgeBase,
  KnowledgeBaseCreateRequest,
  KbDocument,
  LoginRequest,
  TokenStats,
  DailyUsagePoint,
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
  deleteSession: (sessionId: string) =>
    request.delete<ApiResponse<void>>(`/chat/sessions/${sessionId}`, {
      skipGlobalErrorMessage: true,
    }),
  getMessages: (sessionId: string) =>
    request.get<ApiResponse<ChatMessageItem[]>>(`/chat/sessions/${sessionId}/messages`),
};

const evalRequestConfig = { skipGlobalErrorMessage: true };

export const evalApi = {
  runEvaluation: (kbId: number) =>
    request.post<ApiResponse<EvaluationReport>>(
      `/eval/${kbId}/run`,
      null,
      {
        ...evalRequestConfig,
        timeout: 300000, // 评估任务特别长，设置5分钟超时
      },
    ),
  getHistory: (
    kbId: number,
    options: { version?: number; page?: number; pageSize?: number } = {},
  ) =>
    request.get<ApiResponse<EvaluationReport[] | EvaluationHistoryPage>>(`/eval/${kbId}/history`, {
      ...evalRequestConfig,
      params: {
        ...(options.version ? { version: options.version } : {}),
        ...(options.page ? { page: options.page } : {}),
        ...(options.pageSize ? { page_size: options.pageSize } : {}),
      },
    }),
  listDataset: (kbId: number, status?: string) =>
    request.get<ApiResponse<EvalDataset[]>>(`/eval/${kbId}/dataset`, {
      ...evalRequestConfig,
      params: status ? { status } : undefined,
    }),
  addQuestion: (kbId: number, data: EvalDatasetWriteRequest) =>
    request.post<ApiResponse<EvalDataset>>(`/eval/${kbId}/dataset`, data, evalRequestConfig),
  updateQuestion: (kbId: number, id: number, data: EvalDatasetWriteRequest) =>
    request.put<ApiResponse<EvalDataset>>(
      `/eval/${kbId}/dataset/${id}`,
      data,
      evalRequestConfig,
    ),
  deleteQuestion: (kbId: number, id: number) =>
    request.delete<ApiResponse<void>>(`/eval/${kbId}/dataset/${id}`, evalRequestConfig),
  listChunks: (kbId: number) =>
    request.get<ApiResponse<ChunkSummary[]>>(`/eval/${kbId}/chunks`, evalRequestConfig),
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
  getUsageAccess: (config?: AxiosRequestConfig) =>
    request.get<ApiResponse<boolean>>('/stats/usage/access', config),
  getDailyUsage: (days: number, config?: AxiosRequestConfig) =>
    request.get<ApiResponse<DailyUsagePoint[]>>(`/stats/usage/daily?days=${days}`, config),
};
