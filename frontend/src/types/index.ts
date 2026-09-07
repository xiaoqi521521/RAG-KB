export interface ApiResponse<T = unknown> {
  code: number;
  message: string;
  data: T;
}

export interface LoginRequest {
  username: string;
  password: string;
}

export type KbPermission = 'READ' | 'WRITE' | 'ADMIN';

export interface KnowledgeBase {
  id: number;
  name: string;
  description: string | null;
  department_id: string;
  is_public: boolean;
  created_by: number;
  created_at: string | null;
  permission: string;
}

export interface KnowledgeBaseCreateRequest {
  name: string;
  description?: string | null;
  department_id: string;
  is_public: boolean;
}

export interface KbDocument {
  id: number;
  kb_id: number;
  file_name: string;
  file_type: string;
  file_size: number;
  status: 'PENDING' | 'PROCESSING' | 'DONE' | 'FAILED' | string;
  error_msg: string | null;
  chunk_count: number | null;
  token_count: number | null;
  version: number;
  uploaded_by: number;
  uploaded_at: string | null;
  indexed_at: string | null;
}

export interface DocumentUploadResponse {
  doc_id: number;
  file_name: string;
  status: string;
  message: string;
}

export interface IndexStatusResponse {
  doc_id: number;
  file_name: string;
  status: string;
  error_msg: string | null;
  chunk_count: number | null;
  token_count: number | null;
  indexed_at: string | null;
  retry_count: number;
}

export interface SourceCitation {
  reference_index: number;
  document_id: number;
  document_name: string;
  kb_id: number;
  chunk_id: number;
  chunk_index: number;
  page_number: number | null;
  section_title: string | null;
  excerpt: string;
  score: number;
}

export interface RagResponse {
  answer: string;
  sources: SourceCitation[];
  hit_count: number;
  latency_ms: number;
  answer_mode?: string;
  knowledge_base_searched?: boolean;
  notice?: string | null;
}

export interface ChatQueryResponse extends RagResponse {
  session_id: string | null;
}

export interface ChatMessage {
  id: string;
  messageId: number | null;
  role: 'user' | 'assistant';
  content: string;
  sources: SourceCitation[] | null;
  latencyMs: number | null;
  feedback: number | null;
  streaming: boolean;
  status: string | null;
  timestamp: number;
  answerMode?: string;
  knowledgeBaseSearched?: boolean;
  notice?: string | null;
}

export interface ChatSessionItem {
  id: string;
  user_id: number;
  kb_ids: string;
  title: string | null;
  message_count: number;
  created_at: string;
  last_active_at: string;
}

export interface ChatMessageItem {
  id: number;
  session_id: string;
  role: string;
  content: string;
  sources: SourceCitation[] | Record<string, SourceCitation> | null;
  token_count: number | null;
  latency_ms: number | null;
  feedback: number | null;
  created_at: string;
  answer_mode?: string;
  knowledge_base_searched?: boolean;
}

export interface EvaluationReport {
  kb_id: number;
  eval_version: number;
  total_questions: number;
  success_count: number;
  partial_count: number;
  failed_count: number;
  retrieval_sample_count: number;
  hit_count: number;
  hit_rate_at_5: number | null;
  mrr_at_5: number | null;
  faithfulness_sample_count: number;
  avg_faithfulness: number | null;
  answer_relevancy_sample_count: number;
  avg_answer_relevancy: number | null;
  context_recall_sample_count: number;
  avg_context_recall: number | null;
  context_precision_sample_count: number;
  avg_context_precision: number | null;
  refusal_count: number;
  refusal_rate: number;
  duration_ms: number | null;
  eval_at: string;
}

export interface EvaluationHistoryPage {
  items: EvaluationReport[];
  total: number;
  page: number;
  page_size: number;
  versions: number[];
}

export interface TokenStats {
  embedding_tokens: number;
  input_tokens: number;
  answer_generation_tokens: number;
  intent_tokens: number;
  hyde_tokens: number;
  reranker_tokens: number;
  faithfulness_tokens: number;
  estimated_cost: string;
  embedding_cost: string;
  input_cost: string;
  answer_generation_cost: string;
  intent_cost: string;
  hyde_cost: string;
  reranker_cost: string;
  faithfulness_cost: string;
  currency: string;
}

export interface EvalDataset {
  id: number;
  kb_id: number;
  question: string;
  expected_answer: string | null;
  expected_chunk_ids: number[] | null;
  status: string;
  review_reason: string | null;
  source_feedback_id: number | null;
  created_by: number;
  created_at: string;
  updated_at: string;
}

export interface EvalDatasetWriteRequest {
  question: string;
  expected_answer?: string | null;
  expected_chunk_ids?: number[] | null;
  status?: string | null;
}

export interface ChunkSummary {
  chunk_id: number;
  document_id: number;
  document_name: string;
  chunk_index: number;
  page_number: number | null;
  section_title: string | null;
  token_count: number;
  excerpt: string;
}
