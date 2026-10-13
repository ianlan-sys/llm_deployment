export type ProviderMode = 'demo' | 'real'
export type FeatureProviderMode = 'mock' | 'local'
export type FeatureKey = 'call_assistant' | 'knowledge' | 'document_review'
export interface FeatureProviderSettings { feature: FeatureKey; mode: FeatureProviderMode }
export interface FeatureProviderSettingsUpdate { mode: FeatureProviderMode }
export interface AuthPrincipal { username: string }
export interface AuthLoginRequest { username: string; password: string }
export type ReviewTaskStatus = 'queued' | 'parsing' | 'matching_rules' | 'reviewing' | 'summarizing' | 'completed' | 'failed' | 'cancelled' | 'interrupted'
export type ReviewRisk = 'high' | 'medium' | 'low'
export type ReviewDocumentType = 'design_document' | 'technical_proposal' | 'tender_document'

export interface CallSummaryResponse { appeal: string; process: string; result: string; follow_up: string }
export interface CallScenarioResponse { id: string; name: string; description: string; category: string; priority: string; contact_address: string | null; status: string }
export interface CallSuggestionRequest { scenario_id: string; transcript: string }
export interface CallSummaryRequest { session_id: string; scenario_id: string; original_transcript: string; accepted_reply: string }
export interface CallTemplateReference { id: string; title: string; content: string }
export interface CallSuggestionMeta { request_id: string; session_id: string; mode: ProviderMode | FeatureProviderMode; template_ids: string[]; templates: CallTemplateReference[]; started_at: string }
export interface CallSuggestionDelta { content: string }
export interface CallSuggestionDone { request_id: string; elapsed_ms: number }
export interface CallSuggestionError { message: string }
export interface CallSessionResponse { id: string; scenario_id: string; transcript: string; suggestion: string; template_references: CallTemplateReference[]; provider_mode: ProviderMode | FeatureProviderMode; request_id: string; ttft_ms: number | null; elapsed_ms: number | null; adoption_status: 'pending' | 'accepted' | 'edited'; accepted_reply: string | null; summary: CallSummaryResponse | null; status: string; created_at: string; updated_at: string }
export interface KnowledgeQueryRequest { question: string; scope?: string | null }
export interface KnowledgeCitation { chunk_id: string; source_id: string; source_title: string; source_updated_at: string; score: number; text: string; location: string }
export interface KnowledgeQueryResponse { answer: string; citations: KnowledgeCitation[]; status: 'answered' | 'no_evidence'; retrieval_engine: 'numpy' | 'faiss'; disclaimer: string | null }
export interface KnowledgeRetrievalEvent { citations: KnowledgeCitation[]; retrieval_engine: 'numpy' | 'faiss' }
export interface KnowledgeMetaEvent { mode: FeatureProviderMode; model: string }
export interface KnowledgeDeltaEvent { content: string }
export interface KnowledgeDoneEvent { status: 'answered' | 'no_evidence'; answer?: string; disclaimer?: string | null; elapsed_ms: number }
export interface KnowledgeErrorEvent { status: string; message: string }
export type KnowledgeDocumentStatus = 'uploaded' | 'parsing' | 'pending_review' | 'indexing' | 'active' | 'parse_failed' | 'index_failed' | 'rejected' | 'archived'
export interface KnowledgeChunkPreview { id: string; content: string; location: string; position: number }
export interface KnowledgeDocumentSummary {
  id: string; title: string; category: string; scope: string; version: string; effective_date: string | null
  status: KnowledgeDocumentStatus; content_summary: string | null; error_summary: string | null
  reviewed_at: string | null; reviewed_by: string | null; created_at: string; updated_at: string
}
export interface KnowledgeDocumentDetail extends KnowledgeDocumentSummary {
  media_type: string | null; file_size: number | null; parse_warnings: string[]; chunks: KnowledgeChunkPreview[]
}
export interface ReviewFindingResponse { id: string; rule_id: string; rule_source: string; risk: ReviewRisk; description: string; location: string; excerpt: string; recommendation: string; human_review_status: string }
export interface ReviewTaskResponse { id: string; document_filename: string; document_type: string; status: ReviewTaskStatus; stage: string; progress: number; error_summary: string | null; started_at: string | null; completed_at: string | null; created_at: string; updated_at: string; findings: ReviewFindingResponse[] }
export interface ProviderHealth { available: boolean; detail: string }
export interface ProviderSettings { mode: ProviderMode; base_url: string | null; model: string | null }
export interface ProviderSettingsUpdate extends ProviderSettings {}
export interface ProviderConnectionTestRequest extends ProviderSettingsUpdate {}
export interface DashboardResponse { service: string; mode: ProviderMode; today_requests: number; average_latency_ms: number; citation_coverage: number; review_counts: Record<string, number>; queues: Record<string, number> }
export interface AuditEventResponse { id: string; event_type: string; request_summary: string; reference_id: string | null; mode: ProviderMode | FeatureProviderMode; elapsed_ms: number | null; result_status: string; created_at: string }

export type MonitoringSamplingStatus = 'idle' | 'ok' | 'sampling_error'
export interface MonitoringOllamaStatus {
  available: boolean
  installed: boolean
  loaded: boolean
  display_label: 'Qwen3 1.7B Q4'
  version: string | null
  size_bytes: number | null
}
export interface MonitoringInferenceStatus { running: number; queued: number; concurrency: number; max_queue: number }
export interface MonitoringFeatureSnapshot { feature: FeatureKey; mode: FeatureProviderMode }
export interface MonitoringLiveResponse {
  sampling_status: MonitoringSamplingStatus
  ollama: MonitoringOllamaStatus
  inference: MonitoringInferenceStatus
  features: MonitoringFeatureSnapshot[]
}
export interface MonitoringRequestPoint {
  created_at: string
  feature: FeatureKey
  mode: FeatureProviderMode
  result_status: string
  request_count: number
  average_elapsed_ms: number
  p95_elapsed_ms: number
  average_ttft_ms: number | null
  average_queue_wait_ms: number
  input_tokens: number | null
  output_tokens: number | null
}
export interface MonitoringResourcePoint {
  created_at: string
  cpu_percent: number
  memory_percent: number
  disk_percent: number
  ollama_available: boolean
  model_loaded: boolean
  running_count: number
  queued_count: number
}
export interface MonitoringHistoryResponse {
  hours: number
  total_requests: number
  average_elapsed_ms: number
  p95_elapsed_ms: number
  average_ttft_ms: number | null
  average_queue_wait_ms: number
  max_queue_depth: number
  input_tokens: number | null
  output_tokens: number | null
  requests: MonitoringRequestPoint[]
  resources: MonitoringResourcePoint[]
}
