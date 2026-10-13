from datetime import date, datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class Schema(BaseModel):
    model_config = ConfigDict(from_attributes=True, extra="forbid")


class AuthLoginRequest(Schema):
    username: str = Field(min_length=1, max_length=100)
    password: str = Field(min_length=1, max_length=1024)


class AuthSessionResponse(Schema):
    username: str


class CallSummaryResponse(Schema):
    appeal: str
    process: str
    result: str
    follow_up: str


CallSummary = CallSummaryResponse


class CallScenarioResponse(Schema):
    id: str
    name: str
    description: str
    category: str
    priority: str
    contact_address: str | None = None
    status: str


class CallSuggestionRequest(Schema):
    scenario_id: str = Field(min_length=1, max_length=36)
    transcript: str = Field(min_length=1, max_length=10000)


class CallSummaryRequest(Schema):
    session_id: str = Field(min_length=36, max_length=36)
    scenario_id: str = Field(min_length=36, max_length=36)
    original_transcript: str = Field(min_length=1, max_length=10000)
    accepted_reply: str = Field(min_length=1, max_length=10000)


class CallAdoptionRequest(Schema):
    accepted_reply: str = Field(min_length=1, max_length=10000)


class CallTemplateReference(Schema):
    id: str
    title: str
    content: str


class CallSessionResponse(Schema):
    id: str
    scenario_id: str
    transcript: str
    suggestion: str
    template_references: list[CallTemplateReference] = Field(default_factory=list)
    provider_mode: Literal["demo", "real", "mock", "local"]
    request_id: str
    ttft_ms: int | None = Field(default=None, ge=0)
    elapsed_ms: int | None = Field(default=None, ge=0)
    adoption_status: Literal["pending", "accepted", "edited"]
    accepted_reply: str | None = None
    summary: CallSummaryResponse | None = None
    status: str
    created_at: datetime
    updated_at: datetime


class ContextChunk(Schema):
    chunk_id: str
    source_id: str
    text: str
    location: str


class KnowledgeCitation(Schema):
    chunk_id: str
    source_id: str
    location: str


class KnowledgeCitationResponse(KnowledgeCitation):
    source_title: str
    source_updated_at: datetime
    score: float
    text: str


class KnowledgeAnswer(Schema):
    answer: str
    citations: list[KnowledgeCitation] = Field(default_factory=list)


class KnowledgeQueryRequest(Schema):
    question: str = Field(min_length=1, max_length=1000)
    scope: str | None = Field(default=None, max_length=150)


class KnowledgeQueryResponse(Schema):
    answer: str
    citations: list[KnowledgeCitationResponse] = Field(default_factory=list)
    status: Literal["answered", "no_evidence"]
    retrieval_engine: Literal["numpy", "faiss"]
    disclaimer: str | None = None

    @model_validator(mode="after")
    def require_disclaimer_for_answered_response(self) -> "KnowledgeQueryResponse":
        if self.status == "answered" and not self.disclaimer:
            raise ValueError("answered knowledge responses require a disclaimer")
        return self


KnowledgeDocumentStatus = Literal[
    "uploaded",
    "parsing",
    "pending_review",
    "indexing",
    "active",
    "parse_failed",
    "index_failed",
    "rejected",
    "archived",
]


class KnowledgeChunkPreview(Schema):
    id: str
    content: str
    location: str
    position: int


class KnowledgeDocumentSummary(Schema):
    id: str
    title: str
    category: str
    scope: str
    version: str
    effective_date: date | None = None
    status: KnowledgeDocumentStatus
    content_summary: str | None = None
    error_summary: str | None = None
    reviewed_at: datetime | None = None
    reviewed_by: str | None = None
    created_at: datetime
    updated_at: datetime


class KnowledgeDocumentDetail(KnowledgeDocumentSummary):
    media_type: str | None = None
    file_size: int | None = Field(default=None, ge=0)
    parse_warnings: list[str] = Field(default_factory=list)
    chunks: list[KnowledgeChunkPreview] = Field(default_factory=list)


class ReviewRuleInput(Schema):
    id: str
    document_type: str
    check_item: str
    criterion: str
    source: str
    default_risk: str


class ReviewFindingInput(Schema):
    rule_id: str
    risk: Literal["high", "medium", "low"]
    description: str = Field(min_length=1)
    location: str = Field(min_length=1)
    excerpt: str = Field(min_length=1)
    recommendation: str = Field(min_length=1)


ReviewTaskStatus = Literal[
    "queued",
    "parsing",
    "matching_rules",
    "reviewing",
    "summarizing",
    "completed",
    "failed",
    "cancelled",
    "interrupted",
]


class ReviewFindingResponse(Schema):
    id: str
    rule_id: str
    rule_source: str
    risk: Literal["high", "medium", "low"]
    description: str
    location: str
    excerpt: str
    recommendation: str
    human_review_status: str


class ReviewTaskResponse(Schema):
    id: str
    document_filename: str
    document_type: str
    status: ReviewTaskStatus
    stage: str
    progress: int
    error_summary: str | None = None
    started_at: datetime | None = None
    completed_at: datetime | None = None
    created_at: datetime
    updated_at: datetime
    findings: list[ReviewFindingResponse] = Field(default_factory=list)


class ProviderHealth(Schema):
    available: bool
    detail: str


class ProviderSettingsUpdate(Schema):
    mode: Literal["demo", "real"]
    base_url: str | None = None
    model: str | None = None


class ProviderSettingsResponse(Schema):
    mode: Literal["demo", "real"]
    base_url: str | None = None
    model: str | None = None


class ProviderConnectionTestRequest(ProviderSettingsUpdate):
    pass


FeatureKey = Literal["call_assistant", "knowledge", "document_review"]
FeatureMode = Literal["mock", "local"]


class FeatureProviderSettingsUpdate(Schema):
    mode: FeatureMode
    model: str | None = Field(default=None, min_length=1, max_length=100)


class FeatureProviderSettingsResponse(Schema):
    feature: FeatureKey
    mode: FeatureMode
    model: str | None = None


class FeatureProviderConnectionTestRequest(FeatureProviderSettingsUpdate):
    pass


class DashboardResponse(Schema):
    service: str
    mode: FeatureMode
    today_requests: int = Field(ge=0)
    average_latency_ms: int = Field(ge=0)
    citation_coverage: int = Field(ge=0, le=100)
    review_counts: dict[str, int] = Field(default_factory=dict)
    queues: dict[str, int]


class AuditEventResponse(Schema):
    id: str
    event_type: str
    request_summary: str
    reference_id: str | None = None
    mode: Literal["demo", "real", "mock", "local"]
    elapsed_ms: int | None = Field(default=None, ge=0)
    result_status: str
    created_at: datetime


class MonitoringRequestPoint(Schema):
    created_at: datetime
    feature: FeatureKey
    mode: FeatureMode
    result_status: str
    request_count: int = Field(ge=0)
    average_elapsed_ms: float = Field(ge=0)
    p95_elapsed_ms: int = Field(ge=0)
    average_ttft_ms: float | None = Field(default=None, ge=0)
    average_queue_wait_ms: float = Field(ge=0)
    input_tokens: int | None = Field(default=None, ge=0)
    output_tokens: int | None = Field(default=None, ge=0)


class MonitoringResourcePoint(Schema):
    created_at: datetime
    cpu_percent: float = Field(ge=0, le=100)
    memory_percent: float = Field(ge=0, le=100)
    disk_percent: float = Field(ge=0, le=100)
    ollama_available: bool
    model_loaded: bool
    running_count: int = Field(ge=0)
    queued_count: int = Field(ge=0)


class MonitoringHistoryResponse(Schema):
    hours: int = Field(ge=1, le=24)
    total_requests: int = Field(ge=0)
    average_elapsed_ms: float = Field(ge=0)
    p95_elapsed_ms: int = Field(ge=0)
    average_ttft_ms: float | None = Field(default=None, ge=0)
    average_queue_wait_ms: float = Field(ge=0)
    max_queue_depth: int = Field(ge=0)
    input_tokens: int | None = Field(default=None, ge=0)
    output_tokens: int | None = Field(default=None, ge=0)
    requests: list[MonitoringRequestPoint] = Field(default_factory=list)
    resources: list[MonitoringResourcePoint] = Field(default_factory=list)


class MonitoringOllamaStatus(Schema):
    available: bool
    installed: bool
    loaded: bool
    display_label: Literal["Qwen3 1.7B Q4"] = "Qwen3 1.7B Q4"
    version: str | None = Field(default=None, max_length=64)
    size_bytes: int | None = Field(
        default=None,
        ge=0,
        le=10_995_116_277_760,
    )


class MonitoringInferenceStatus(Schema):
    running: int = Field(ge=0)
    queued: int = Field(ge=0)
    concurrency: int = Field(ge=0)
    max_queue: int = Field(ge=0)


class MonitoringFeatureSnapshot(Schema):
    feature: FeatureKey
    mode: FeatureMode


class MonitoringLiveResponse(Schema):
    sampling_status: Literal["idle", "ok", "sampling_error"]
    ollama: MonitoringOllamaStatus
    inference: MonitoringInferenceStatus
    features: list[MonitoringFeatureSnapshot]
