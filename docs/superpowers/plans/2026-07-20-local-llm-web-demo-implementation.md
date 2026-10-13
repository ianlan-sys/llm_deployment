# Local LLM Web Demo Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build an offline-capable React and FastAPI Web demo that presents call-center assistance, cited operations knowledge Q&A, asynchronous document review, and an optional OpenAI-compatible real-model connection.

**Architecture:** A React/TypeScript/Vite SPA calls a FastAPI API through REST and SSE. FastAPI owns business services, provider adapters, local retrieval, document jobs, SQLite persistence, and audit records; the browser never connects to a model directly. The same repository runs through a Windows launcher or Docker Compose.

**Tech Stack:** React, TypeScript, Vite, React Router, TanStack Query, Vitest, Testing Library, Playwright, FastAPI, Pydantic v2, SQLAlchemy 2, SQLite, httpx, pypdf, python-docx, NumPy, optional FAISS, pytest.

## Global Constraints

- The first release is a single-user desktop demo with no login page.
- The UI uses the approved management-dashboard layout and government deep-blue A1 visual direction.
- Demo mode must run without internet, model weights, or third-party services.
- Real mode must support Ollama, vLLM, MindIE, and other OpenAI-compatible endpoints without changing the frontend workflow.
- Supported review uploads are PDF, DOCX, and TXT, with a 20 MB maximum per file.
- Model output is advisory; the UI must retain human-review and no-device-control disclaimers.
- The browser must never receive or persist a model API key.
- Windows one-click startup and Docker Compose startup are both required.
- Desktop layouts must remain operable at 1366×768 and larger.
- User-visible copy is Simplified Chinese; code identifiers and API fields are English.

---

## File Structure

```text
backend/
  pyproject.toml                       Python dependencies and pytest config
  app/
    __init__.py
    main.py                            FastAPI construction and router assembly
    config.py                          Environment and runtime paths
    database.py                        SQLAlchemy engine/session/base
    models.py                          SQLite ORM entities
    schemas.py                         Shared Pydantic API DTOs
    seed.py                            Idempotent fictional data seeding
    api/
      dashboard.py                    Dashboard and audit endpoints
      settings.py                     Provider settings and connection test
      calls.py                         Call scenarios, SSE suggestions, summaries
      knowledge.py                     Cited knowledge-query endpoint
      reviews.py                       Upload, task state, retry, cancel, export
    providers/
      base.py                          AIProvider protocol and provider exceptions
      mock.py                          Deterministic offline provider
      openai_compatible.py             OpenAI-compatible HTTP adapter
      registry.py                      Active provider selection
    retrieval/
      index.py                         VectorIndex protocol and search result type
      hashing.py                       Deterministic feature-hash embeddings
      numpy_index.py                   NumPy cosine index
      faiss_index.py                   Optional FAISS implementation
      service.py                       Scope filtering, ranking, keyword fallback
    services/
      audit.py                         Audit event writer
      call_assistant.py                Call-template retrieval and provider orchestration
      knowledge.py                     Answer construction and citation validation
      review_parser.py                 PDF, DOCX, and TXT parsing
      review_pipeline.py               Review task state machine and finding merge
      review_executor.py               Controlled in-process job execution
    data/
      call_scenarios.json              Four fictional calls
      call_templates.json              Twelve approved scripts
      knowledge_sources.json           Six sources and four change records
      review_rules.json                Twenty-four review rules
      review_samples/                  Fictional proposal and tender samples
  tests/
    conftest.py
    test_health.py
    test_seed.py
    test_providers.py
    test_settings_api.py
    test_retrieval.py
    test_knowledge_api.py
    test_calls_api.py
    test_review_parser.py
    test_review_pipeline.py
    test_reviews_api.py
    test_dashboard_api.py
frontend/
  package.json
  pnpm-lock.yaml
  vite.config.ts
  tsconfig.json
  index.html
  src/
    main.tsx
    app/App.tsx
    app/router.tsx
    app/queryClient.ts
    api/client.ts
    api/types.ts
    styles/tokens.css
    styles/global.css
    components/AppShell.tsx
    components/StatusBadge.tsx
    components/Disclaimer.tsx
    components/SourceDrawer.tsx
    components/ErrorPanel.tsx
    pages/DashboardPage.tsx
    pages/CallAssistantPage.tsx
    pages/KnowledgePage.tsx
    pages/ReviewPage.tsx
    pages/SettingsPage.tsx
    test/setup.ts
    test/AppShell.test.tsx
    test/DashboardPage.test.tsx
    test/CallAssistantPage.test.tsx
    test/KnowledgePage.test.tsx
    test/ReviewPage.test.tsx
    test/SettingsPage.test.tsx
  e2e/demo-flow.spec.ts
  playwright.config.ts
docker/
  backend.Dockerfile
  frontend.Dockerfile
  nginx.conf
scripts/
  start-demo.ps1
  stop-demo.ps1
.env.example
docker-compose.yml
README.md
```

### Task 1: Backend Foundation and Health Contract

**Files:**
- Create: `backend/pyproject.toml`
- Create: `backend/app/__init__.py`
- Create: `backend/app/config.py`
- Create: `backend/app/main.py`
- Create: `backend/tests/conftest.py`
- Create: `backend/tests/test_health.py`

**Interfaces:**
- Produces: `create_app(settings: Settings | None = None) -> FastAPI`
- Produces: `Settings` with `runtime_dir`, `database_url`, `provider_mode`, `max_upload_bytes`, and `cors_origins`
- Produces: `GET /api/health -> {status, mode, version}`

- [ ] **Step 1: Write the failing health test**

```python
from fastapi.testclient import TestClient
from app.config import Settings
from app.main import create_app


def test_health_reports_demo_mode(tmp_path):
    settings = Settings(runtime_dir=tmp_path, provider_mode="demo")
    with TestClient(create_app(settings)) as client:
        response = client.get("/api/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok", "mode": "demo", "version": "0.1.0"}
```

- [ ] **Step 2: Run the test and verify the missing application failure**

Run: `cd backend && python -m pytest tests/test_health.py -v`

Expected: FAIL because `app.config` and `app.main` do not exist.

- [ ] **Step 3: Add project configuration and the minimal application factory**

```python
# backend/app/config.py
from pathlib import Path
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="DEMO_", extra="ignore")
    runtime_dir: Path = Path("data/runtime")
    database_url: str | None = None
    provider_mode: str = "demo"
    max_upload_bytes: int = 20 * 1024 * 1024
    cors_origins: list[str] = ["http://localhost:5173", "http://localhost:8080"]

    def resolved_database_url(self) -> str:
        return self.database_url or f"sqlite:///{self.runtime_dir / 'demo.db'}"
```

```python
# backend/app/main.py
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from app.config import Settings


def create_app(settings: Settings | None = None) -> FastAPI:
    cfg = settings or Settings()
    cfg.runtime_dir.mkdir(parents=True, exist_ok=True)
    app = FastAPI(title="本地化大模型应用平台", version="0.1.0")
    app.state.settings = cfg
    app.add_middleware(
        CORSMiddleware,
        allow_origins=cfg.cors_origins,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    @app.get("/api/health")
    def health() -> dict[str, str]:
        return {"status": "ok", "mode": cfg.provider_mode, "version": "0.1.0"}

    return app


app = create_app()
```

Set `backend/pyproject.toml` dependencies to FastAPI, Uvicorn, Pydantic Settings, SQLAlchemy, httpx, python-multipart, pypdf, python-docx, NumPy, pytest, pytest-asyncio, and ruff. Configure pytest with `pythonpath = ["."]`.

- [ ] **Step 4: Run the focused test and lint check**

Run: `cd backend && python -m pytest tests/test_health.py -v && python -m ruff check app tests`

Expected: one passing test and no Ruff errors.

- [ ] **Step 5: Commit the backend foundation**

```powershell
git add backend/pyproject.toml backend/app backend/tests
git commit -m "feat: bootstrap FastAPI backend"
```

### Task 2: Persistence and Fictional Seed Data

**Files:**
- Create: `backend/app/database.py`
- Create: `backend/app/models.py`
- Create: `backend/app/schemas.py`
- Create: `backend/app/seed.py`
- Create: `backend/app/data/call_scenarios.json`
- Create: `backend/app/data/call_templates.json`
- Create: `backend/app/data/knowledge_sources.json`
- Create: `backend/app/data/review_rules.json`
- Create: `backend/app/data/review_samples/technical-proposal.txt`
- Create: `backend/app/data/review_samples/tender-document.txt`
- Create: `backend/tests/test_seed.py`
- Modify: `backend/app/main.py`
- Modify: `backend/tests/conftest.py`

**Interfaces:**
- Produces: `get_session() -> Iterator[Session]`
- Produces: ORM entities `AppSetting`, `CallScenario`, `CallTemplate`, `KnowledgeSource`, `KnowledgeChunk`, `ReviewRule`, `ReviewTask`, `ReviewFinding`, and `AuditEvent`
- Produces: `seed_demo_data(session: Session) -> None`, which is idempotent

- [ ] **Step 1: Write the failing idempotent-seed test**

```python
from sqlalchemy import select, func
from app.models import CallScenario, CallTemplate, KnowledgeSource, ReviewRule
from app.seed import seed_demo_data


def test_seed_is_idempotent(db_session):
    seed_demo_data(db_session)
    seed_demo_data(db_session)
    assert db_session.scalar(select(func.count()).select_from(CallScenario)) == 4
    assert db_session.scalar(select(func.count()).select_from(CallTemplate)) == 12
    assert db_session.scalar(select(func.count()).select_from(KnowledgeSource)) == 10
    assert db_session.scalar(select(func.count()).select_from(ReviewRule)) == 24
```

- [ ] **Step 2: Run the seed test and verify it fails**

Run: `cd backend && python -m pytest tests/test_seed.py -v`

Expected: FAIL because persistence modules do not exist.

- [ ] **Step 3: Implement database setup, entities, fixtures, and startup seeding**

Define string IDs as UUID text, UTC timestamps, explicit status strings, and foreign keys. Store provider secrets nowhere in the schema. Build fictional source data with RFC 5737 addresses such as `192.0.2.10` and `198.51.100.20`. Each review rule must contain `document_type`, `check_item`, `criterion`, `source`, and `default_risk`.

```python
def seed_demo_data(session: Session) -> None:
    if session.scalar(select(func.count()).select_from(CallScenario)):
        return
    session.add_all(load_call_scenarios())
    session.add_all(load_call_templates())
    sources, chunks = load_knowledge_sources()
    session.add_all([*sources, *chunks])
    session.add_all(load_review_rules())
    session.commit()
```

Register an application lifespan that creates tables and runs `seed_demo_data` exactly once per empty database. Update the test fixture to use a temporary SQLite database with `check_same_thread=False`.

- [ ] **Step 4: Run seed and health regression tests**

Run: `cd backend && python -m pytest tests/test_seed.py tests/test_health.py -v`

Expected: all tests pass; the second seed call does not duplicate rows.

- [ ] **Step 5: Commit persistence and fixtures**

```powershell
git add backend/app backend/tests
git commit -m "feat: add demo persistence and seed data"
```

### Task 3: Provider Abstraction and Settings API

**Files:**
- Create: `backend/app/providers/base.py`
- Create: `backend/app/providers/mock.py`
- Create: `backend/app/providers/openai_compatible.py`
- Create: `backend/app/providers/registry.py`
- Create: `backend/app/api/settings.py`
- Create: `backend/tests/test_providers.py`
- Create: `backend/tests/test_settings_api.py`
- Modify: `backend/app/main.py`
- Modify: `backend/app/schemas.py`

**Interfaces:**
- Produces: `AIProvider` protocol methods `health_check`, `stream_call_suggestion`, `generate_call_summary`, `answer_with_context`, and `review_section`
- Produces: `ProviderRegistry.get_active() -> AIProvider`
- Produces: provider settings GET, PUT, and connection-test endpoints

- [ ] **Step 1: Write provider contract and secret-persistence tests**

```python
def test_mock_provider_is_deterministic():
    provider = MockProvider(delay_ms=0)
    first = list(provider.stream_call_suggestion(call_context={"intent": "故障报修"}, templates=[]))
    second = list(provider.stream_call_suggestion(call_context={"intent": "故障报修"}, templates=[]))
    assert first == second
    assert "故障" in "".join(first)


def test_settings_response_never_returns_api_key(client):
    response = client.put("/api/settings/provider", json={
        "mode": "real",
        "base_url": "http://127.0.0.1:8001/v1",
        "model": "local-model",
        "api_key": "super-secret",
    })
    assert response.status_code == 200
    assert "super-secret" not in response.text
    assert "api_key" not in response.json()
```

- [ ] **Step 2: Run provider tests and verify missing modules**

Run: `cd backend && python -m pytest tests/test_providers.py tests/test_settings_api.py -v`

Expected: FAIL because provider modules and settings routes do not exist.

- [ ] **Step 3: Implement provider interfaces, adapters, and registry**

```python
class AIProvider(Protocol):
    def health_check(self) -> ProviderHealth: ...
    def stream_call_suggestion(self, call_context: dict, templates: list[dict]) -> Iterator[str]: ...
    def generate_call_summary(self, transcript: str) -> CallSummary: ...
    def answer_with_context(self, question: str, chunks: list[ContextChunk]) -> KnowledgeAnswer: ...
    def review_section(self, document_type: str, section: str, rules: list[ReviewRuleInput]) -> list[ReviewFindingInput]: ...
```

The mock implementation selects deterministic Chinese responses by intent and keyword. The real adapter uses `httpx.Client` against `/chat/completions`, validates timeouts, converts HTTP errors to `ProviderUnavailable`, and validates structured JSON with Pydantic. Store the transient API key only in `ProviderRegistry` memory. Persist mode, Base URL, and model in `AppSetting`.

- [ ] **Step 4: Run provider and settings tests**

Run: `cd backend && python -m pytest tests/test_providers.py tests/test_settings_api.py -v`

Expected: all tests pass, including the response-body secret assertion.

- [ ] **Step 5: Commit provider support**

```powershell
git add backend/app/providers backend/app/api/settings.py backend/app/main.py backend/app/schemas.py backend/tests
git commit -m "feat: add demo and OpenAI-compatible providers"
```

### Task 4: Retrieval and Cited Knowledge Q&A

**Files:**
- Create: `backend/app/retrieval/index.py`
- Create: `backend/app/retrieval/hashing.py`
- Create: `backend/app/retrieval/numpy_index.py`
- Create: `backend/app/retrieval/faiss_index.py`
- Create: `backend/app/retrieval/service.py`
- Create: `backend/app/services/knowledge.py`
- Create: `backend/app/api/knowledge.py`
- Create: `backend/tests/test_retrieval.py`
- Create: `backend/tests/test_knowledge_api.py`
- Modify: `backend/app/main.py`
- Modify: `backend/app/schemas.py`

**Interfaces:**
- Produces: `SearchResult(chunk_id: str, score: float, source_id: str, text: str, location: str)`
- Produces: `RetrievalService.search(query: str, scope: str | None, limit: int = 5) -> list[SearchResult]`
- Produces: `POST /api/knowledge/query -> KnowledgeQueryResponse`

- [ ] **Step 1: Write retrieval and no-evidence API tests**

```python
def test_retrieval_respects_scope(retrieval_service):
    results = retrieval_service.search("核心交换机 VLAN", scope="核心节点A", limit=5)
    assert results
    assert all(result.source_id.startswith("core-a-") for result in results)


def test_knowledge_query_refuses_without_evidence(client):
    response = client.post("/api/knowledge/query", json={"question": "月球节点密码是什么", "scope": None})
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "no_evidence"
    assert body["answer"] == "知识库中暂无可靠依据，请调整关键词或资料范围。"
    assert body["citations"] == []
```

- [ ] **Step 2: Run retrieval tests and verify they fail**

Run: `cd backend && python -m pytest tests/test_retrieval.py tests/test_knowledge_api.py -v`

Expected: FAIL because retrieval and knowledge services do not exist.

- [ ] **Step 3: Implement deterministic hashing, index fallback, and cited answers**

Use stable SHA-256 feature hashing into a fixed 384-dimensional vector and L2 normalization. `FaissVectorIndex` imports FAISS lazily; `NumpyVectorIndex` is always available. Combine cosine similarity with a token-overlap score, then apply a fixed minimum evidence threshold. Validate every provider citation against returned chunk IDs; discard invalid citations and audit the mismatch.

```python
if not results or results[0].score < self.minimum_score:
    return KnowledgeQueryResponse(
        status="no_evidence",
        answer="知识库中暂无可靠依据，请调整关键词或资料范围。",
        citations=[],
        retrieval_engine=self.engine_name,
    )
```

- [ ] **Step 4: Run focused tests and the complete backend suite**

Run: `cd backend && python -m pytest tests/test_retrieval.py tests/test_knowledge_api.py -v && python -m pytest -q`

Expected: cited-answer and no-evidence tests pass; no earlier test regresses.

- [ ] **Step 5: Commit cited knowledge Q&A**

```powershell
git add backend/app/retrieval backend/app/services/knowledge.py backend/app/api/knowledge.py backend/app/main.py backend/app/schemas.py backend/tests
git commit -m "feat: add cited operations knowledge queries"
```

### Task 5: Call Assistant SSE and Work-Order Summary

**Files:**
- Create: `backend/app/services/call_assistant.py`
- Create: `backend/app/services/audit.py`
- Create: `backend/app/api/calls.py`
- Create: `backend/tests/test_calls_api.py`
- Modify: `backend/app/main.py`
- Modify: `backend/app/schemas.py`

**Interfaces:**
- Produces: `GET /api/demo/call-scenarios`
- Produces: `POST /api/calls/suggestions/stream` as SSE events `meta`, `delta`, `done`, or `error`
- Produces: `POST /api/calls/summary -> CallSummaryResponse`

- [ ] **Step 1: Write failing scenario, SSE, and summary tests**

```python
def test_call_suggestion_stream_has_meta_delta_and_done(client):
    with client.stream("POST", "/api/calls/suggestions/stream", json={
        "scenario_id": "fault-report",
        "transcript": "用户反映核心业务访问缓慢",
    }) as response:
        payload = "".join(response.iter_text())
    assert response.status_code == 200
    assert "event: meta" in payload
    assert "event: delta" in payload
    assert "event: done" in payload
    assert "template_ids" in payload


def test_summary_has_required_fields(client):
    response = client.post("/api/calls/summary", json={"transcript": "用户报修，已登记并告知处理时限。"})
    assert response.status_code == 200
    assert set(response.json()) == {"appeal", "process", "result", "follow_up"}
```

- [ ] **Step 2: Run call tests and verify missing endpoint failures**

Run: `cd backend && python -m pytest tests/test_calls_api.py -v`

Expected: FAIL with 404 responses.

- [ ] **Step 3: Implement template retrieval, SSE formatting, summaries, and audits**

Retrieve templates by scenario and keyword, call the active provider, and emit JSON payloads with `event:` and `data:` lines. The `meta` event includes a request ID, provider mode, retrieval template IDs, and start time. The `done` event includes elapsed milliseconds. On disconnect, stop iterating and record a cancelled audit result.

```python
def sse(event: str, data: dict) -> str:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"
```

- [ ] **Step 4: Run call tests and backend regression tests**

Run: `cd backend && python -m pytest tests/test_calls_api.py -v && python -m pytest -q`

Expected: SSE ordering and summary schema tests pass.

- [ ] **Step 5: Commit call assistance**

```powershell
git add backend/app/services backend/app/api/calls.py backend/app/main.py backend/app/schemas.py backend/tests/test_calls_api.py
git commit -m "feat: add streaming call assistance"
```

### Task 6: Document Parsing and Review Task Pipeline

**Files:**
- Create: `backend/app/services/review_parser.py`
- Create: `backend/app/services/review_pipeline.py`
- Create: `backend/app/services/review_executor.py`
- Create: `backend/app/api/reviews.py`
- Create: `backend/tests/test_review_parser.py`
- Create: `backend/tests/test_review_pipeline.py`
- Create: `backend/tests/test_reviews_api.py`
- Modify: `backend/app/main.py`
- Modify: `backend/app/schemas.py`

**Interfaces:**
- Produces: `parse_document(path: Path, media_type: str) -> ParsedDocument`
- Produces: task states `queued`, `parsing`, `matching_rules`, `reviewing`, `summarizing`, `completed`, `failed`, `cancelled`, and `interrupted`
- Produces: review CRUD, retry, cancel, and CSV export endpoints

- [ ] **Step 1: Write parser, state-transition, upload, and export tests**

```python
def test_plain_text_parser_returns_sections(tmp_path):
    path = tmp_path / "proposal.txt"
    path.write_text("一、项目范围\n缺少验收指标。\n二、实施计划\n计划为四周。", encoding="utf-8")
    parsed = parse_document(path, "text/plain")
    assert [section.title for section in parsed.sections] == ["一、项目范围", "二、实施计划"]


def test_invalid_transition_is_rejected():
    task = ReviewTaskState(status="queued", progress=0)
    with pytest.raises(InvalidTaskTransition):
        task.transition("completed", progress=100)


def test_rejects_oversized_upload(client, monkeypatch):
    monkeypatch.setattr(client.app.state.settings, "max_upload_bytes", 10)
    response = client.post("/api/reviews", data={"document_type": "technical_proposal"}, files={"file": ("a.txt", b"12345678901", "text/plain")})
    assert response.status_code == 413
```

- [ ] **Step 2: Run review tests and verify missing service failures**

Run: `cd backend && python -m pytest tests/test_review_parser.py tests/test_review_pipeline.py tests/test_reviews_api.py -v`

Expected: FAIL because parser, state machine, and endpoints do not exist.

- [ ] **Step 3: Implement format validation, parsers, pipeline, and executor**

Validate extension, MIME type, byte size, and non-empty content. PDF parsing uses pypdf; DOCX parsing uses python-docx; TXT uses UTF-8 with a clear decoding error. Split sections on Chinese numbered headings and cap each section by character count. The executor uses a bounded `ThreadPoolExecutor(max_workers=2)` and persists every stage before work begins.

For each section, retrieve applicable rules, call `review_section`, validate findings, and merge duplicates by normalized location plus rule ID. On structured-output failure, issue exactly one repair call. A second failure marks the task failed. At startup, change unfinished active states to `interrupted`.

- [ ] **Step 4: Implement API status, retry, cancel, and UTF-8 BOM CSV export**

The create endpoint returns HTTP 202 and a task resource. Retry creates a new execution attempt on the same task only from `failed`, `cancelled`, or `interrupted`. CSV headers are `风险等级,问题描述,原文位置,原文摘录,规则出处,修改建议,人工复核状态` and the response begins with a UTF-8 BOM for spreadsheet compatibility.

- [ ] **Step 5: Run review tests and full backend suite**

Run: `cd backend && python -m pytest tests/test_review_parser.py tests/test_review_pipeline.py tests/test_reviews_api.py -v && python -m pytest -q`

Expected: parser, transition, upload, retry, cancel, and CSV tests pass.

- [ ] **Step 6: Commit document review support**

```powershell
git add backend/app/services backend/app/api/reviews.py backend/app/main.py backend/app/schemas.py backend/tests
git commit -m "feat: add asynchronous document review"
```

### Task 7: Dashboard and Audit API

**Files:**
- Create: `backend/app/api/dashboard.py`
- Create: `backend/tests/test_dashboard_api.py`
- Modify: `backend/app/main.py`
- Modify: `backend/app/schemas.py`

**Interfaces:**
- Produces: `GET /api/dashboard -> DashboardResponse`
- Produces: `GET /api/audit-events?limit=50 -> list[AuditEventResponse]`

- [ ] **Step 1: Write failing dashboard and audit redaction tests**

```python
def test_dashboard_contains_required_metrics(client):
    body = client.get("/api/dashboard").json()
    assert set(body) == {"service", "mode", "today_requests", "average_latency_ms", "citation_coverage", "review_counts", "queues"}
    assert set(body["queues"]) == {"realtime", "long_document"}


def test_audit_events_do_not_expose_inputs_or_secrets(client):
    events = client.get("/api/audit-events?limit=50").json()
    serialized = json.dumps(events, ensure_ascii=False)
    assert "api_key" not in serialized
    assert "super-secret" not in serialized
```

- [ ] **Step 2: Run tests and verify endpoint failures**

Run: `cd backend && python -m pytest tests/test_dashboard_api.py -v`

Expected: FAIL with 404 responses.

- [ ] **Step 3: Implement aggregated metrics and redacted audit responses**

Use the current UTC date for request counts, completed knowledge events for citation coverage, and review-task statuses for queue counts. Expose only request summaries and IDs, never full uploaded content, full transcripts, or credentials.

- [ ] **Step 4: Run the complete backend suite**

Run: `cd backend && python -m pytest -q && python -m ruff check app tests`

Expected: all backend tests and lint checks pass.

- [ ] **Step 5: Commit dashboard endpoints**

```powershell
git add backend/app/api/dashboard.py backend/app/main.py backend/app/schemas.py backend/tests/test_dashboard_api.py
git commit -m "feat: add dashboard and audit APIs"
```

### Task 8: Frontend Foundation, Design Tokens, and Application Shell

**Files:**
- Create: `frontend/package.json`
- Create: `frontend/pnpm-lock.yaml`
- Create: `frontend/vite.config.ts`
- Create: `frontend/tsconfig.json`
- Create: `frontend/index.html`
- Create: `frontend/src/main.tsx`
- Create: `frontend/src/app/App.tsx`
- Create: `frontend/src/app/router.tsx`
- Create: `frontend/src/app/queryClient.ts`
- Create: `frontend/src/api/client.ts`
- Create: `frontend/src/api/types.ts`
- Create: `frontend/src/styles/tokens.css`
- Create: `frontend/src/styles/global.css`
- Create: `frontend/src/components/AppShell.tsx`
- Create: `frontend/src/components/StatusBadge.tsx`
- Create: `frontend/src/components/Disclaimer.tsx`
- Create: `frontend/src/components/ErrorPanel.tsx`
- Create: `frontend/src/test/setup.ts`
- Create: `frontend/src/test/AppShell.test.tsx`

**Interfaces:**
- Produces: routes `/`, `/calls`, `/knowledge`, `/reviews`, and `/settings`
- Produces: `api.get<T>`, `api.post<T>`, `api.put<T>`, and `api.streamSse`
- Produces: shared TypeScript types matching backend DTOs

- [ ] **Step 1: Scaffold Vite and install runtime and test dependencies**

Run:

```powershell
pnpm create vite frontend --template react-ts
cd frontend
pnpm add react-router-dom @tanstack/react-query
pnpm add -D vitest jsdom @testing-library/react @testing-library/jest-dom @testing-library/user-event
```

Expected: Vite creates the project and `pnpm-lock.yaml` pins resolved versions.

- [ ] **Step 2: Write the failing shell-navigation test**

```tsx
it("renders the five approved navigation entries", () => {
  render(<MemoryRouter><AppShell><div>内容</div></AppShell></MemoryRouter>);
  for (const name of ["工作总览", "坐席辅助", "运维知识库", "文档审核", "运行与设置"]) {
    expect(screen.getByRole("link", { name })).toBeInTheDocument();
  }
});
```

- [ ] **Step 3: Run the test and verify the missing shell failure**

Run: `cd frontend && pnpm vitest run src/test/AppShell.test.tsx`

Expected: FAIL because `AppShell` and test setup are not implemented.

- [ ] **Step 4: Implement the shell, router, API client, types, and A1 tokens**

Use CSS variables `--nav-900: #0d3158`, `--nav-700: #174f7d`, `--primary-600: #1e6fa8`, `--success-600: #1d8b69`, `--warning-600: #d98b28`, `--danger-600: #b8433a`, `--surface: #ffffff`, and `--canvas: #f4f7fa`. The shell collapses labels only below 1100 px but keeps icons and accessible names. Every page has a persistent advisory disclaimer component.

- [ ] **Step 5: Run frontend unit tests and production build**

Run: `cd frontend && pnpm vitest run && pnpm build`

Expected: shell test passes and Vite emits `dist/` without TypeScript errors.

- [ ] **Step 6: Commit the frontend foundation**

```powershell
git add frontend
git commit -m "feat: add React application shell"
```

### Task 9: Dashboard and Provider Settings Pages

**Files:**
- Create: `frontend/src/pages/DashboardPage.tsx`
- Create: `frontend/src/pages/SettingsPage.tsx`
- Create: `frontend/src/test/DashboardPage.test.tsx`
- Create: `frontend/src/test/SettingsPage.test.tsx`
- Modify: `frontend/src/app/router.tsx`
- Modify: `frontend/src/api/types.ts`

**Interfaces:**
- Consumes: `GET /api/dashboard`, `GET /api/audit-events`, and provider settings GET/PUT/test
- Produces: dashboard cards, queue indicators, mode badge, connection form, test status, and a redacted recent-audit table

- [ ] **Step 1: Write failing page behavior tests**

```tsx
it("shows all three business entry cards", async () => {
  server.use(http.get("/api/dashboard", () => HttpResponse.json(dashboardFixture)));
  renderPage(<DashboardPage />);
  expect(await screen.findByText("坐席辅助")).toBeInTheDocument();
  expect(screen.getByText("运维知识库")).toBeInTheDocument();
  expect(screen.getByText("文档审核")).toBeInTheDocument();
});

it("does not render a saved API key", async () => {
  renderPage(<SettingsPage />);
  expect(await screen.findByLabelText("API 密钥（仅本次运行）")).toHaveValue("");
});

it("shows redacted recent audit events", async () => {
  renderPage(<SettingsPage />);
  expect(await screen.findByText("最近审计记录")).toBeInTheDocument();
  expect(screen.getByText("knowledge.query.completed")).toBeInTheDocument();
  expect(screen.queryByText("完整用户问题正文")).not.toBeInTheDocument();
});
```

- [ ] **Step 2: Run page tests and verify missing component failures**

Run: `cd frontend && pnpm vitest run src/test/DashboardPage.test.tsx src/test/SettingsPage.test.tsx`

Expected: FAIL because pages do not exist.

- [ ] **Step 3: Implement dashboard and settings pages**

Use TanStack Query for loading, error, and refresh states. The dashboard displays service health, mode, four metrics, two queue counters, and three navigation cards. Settings requires a successful connection test before activating real mode. Show the retrieval engine, a visible `演示数据` badge in demo mode, and the 50 most recent redacted audit events with event type, result, mode, latency, and timestamp.

- [ ] **Step 4: Run page tests and build**

Run: `cd frontend && pnpm vitest run src/test/DashboardPage.test.tsx src/test/SettingsPage.test.tsx && pnpm build`

Expected: both tests and production build pass.

- [ ] **Step 5: Commit dashboard and settings UI**

```powershell
git add frontend/src
git commit -m "feat: add dashboard and provider settings UI"
```

### Task 10: Call Assistant and Knowledge Pages

**Files:**
- Create: `frontend/src/pages/CallAssistantPage.tsx`
- Create: `frontend/src/pages/KnowledgePage.tsx`
- Create: `frontend/src/components/SourceDrawer.tsx`
- Create: `frontend/src/test/CallAssistantPage.test.tsx`
- Create: `frontend/src/test/KnowledgePage.test.tsx`
- Modify: `frontend/src/app/router.tsx`
- Modify: `frontend/src/api/types.ts`

**Interfaces:**
- Consumes: call scenario, SSE suggestion, summary, and knowledge-query APIs
- Produces: copy/accept/stop call controls and citation source drawer

- [ ] **Step 1: Write failing interaction tests**

```tsx
it("accepts a streamed suggestion and enables summary", async () => {
  renderPage(<CallAssistantPage />);
  await userEvent.selectOptions(await screen.findByLabelText("来电场景"), "fault-report");
  await userEvent.click(screen.getByRole("button", { name: "生成推荐话术" }));
  expect(await screen.findByText(/已为您登记故障/)).toBeInTheDocument();
  await userEvent.click(screen.getByRole("button", { name: "采纳话术" }));
  expect(screen.getByRole("button", { name: "生成工单摘要" })).toBeEnabled();
});

it("opens the cited source drawer", async () => {
  renderPage(<KnowledgePage />);
  await userEvent.type(screen.getByLabelText("运维问题"), "核心交换机 VLAN 如何划分？");
  await userEvent.click(screen.getByRole("button", { name: "查询" }));
  await userEvent.click(await screen.findByRole("button", { name: "来源 1" }));
  expect(screen.getByRole("dialog", { name: "资料出处" })).toBeInTheDocument();
});
```

- [ ] **Step 2: Run tests and verify missing page failures**

Run: `cd frontend && pnpm vitest run src/test/CallAssistantPage.test.tsx src/test/KnowledgePage.test.tsx`

Expected: FAIL because the pages and drawer do not exist.

- [ ] **Step 3: Implement call workspace and SSE lifecycle**

Use a three-column desktop layout. Track `idle`, `streaming`, `completed`, `stopped`, and `error` states. AbortController powers stop generation and route-unmount cleanup. Copy uses `navigator.clipboard`; accepted text remains editable. Display TTFT and provider mode from SSE metadata.

- [ ] **Step 4: Implement knowledge query, no-evidence state, and source drawer**

Render answer citations as buttons mapped by chunk ID. The source drawer shows source title, location, updated time, score, and exact chunk text. The no-evidence state renders the backend message and no answer card. Show the no-device-control disclaimer beside configuration suggestions.

- [ ] **Step 5: Run tests and build**

Run: `cd frontend && pnpm vitest run src/test/CallAssistantPage.test.tsx src/test/KnowledgePage.test.tsx && pnpm build`

Expected: interaction tests and TypeScript build pass.

- [ ] **Step 6: Commit call and knowledge UI**

```powershell
git add frontend/src
git commit -m "feat: add call and knowledge workspaces"
```

### Task 11: Document Review Page

**Files:**
- Create: `frontend/src/pages/ReviewPage.tsx`
- Create: `frontend/src/test/ReviewPage.test.tsx`
- Modify: `frontend/src/app/router.tsx`
- Modify: `frontend/src/api/types.ts`

**Interfaces:**
- Consumes: review create/list/status/retry/cancel/export APIs
- Produces: upload validation, progress timeline, risk filters, finding detail, retry/cancel/export actions

- [ ] **Step 1: Write failing upload and completed-result tests**

```tsx
it("shows asynchronous review stages and findings", async () => {
  renderPage(<ReviewPage />);
  const file = new File(["技术方案正文"], "方案.txt", { type: "text/plain" });
  await userEvent.upload(screen.getByLabelText("选择待审文件"), file);
  await userEvent.selectOptions(screen.getByLabelText("文件类型"), "technical_proposal");
  await userEvent.click(screen.getByRole("button", { name: "开始审核" }));
  expect(await screen.findByText("规则匹配")).toBeInTheDocument();
  expect(await screen.findByText("审核完成")).toBeInTheDocument();
  expect(screen.getByText("高风险")).toBeInTheDocument();
  expect(screen.getByRole("button", { name: "导出审核清单" })).toBeEnabled();
});
```

- [ ] **Step 2: Run the test and verify the missing page failure**

Run: `cd frontend && pnpm vitest run src/test/ReviewPage.test.tsx`

Expected: FAIL because `ReviewPage` does not exist.

- [ ] **Step 3: Implement upload, polling, timeline, filters, and detail panel**

Client-side validation matches backend formats and 20 MB limit. Poll active tasks every second and stop on terminal states. Preserve failed task details and show retry only for `failed`, `cancelled`, or `interrupted`. Findings can filter by high, medium, low, and human-review state. Detail exposes the original excerpt, rule source, recommendation, and advisory disclaimer.

- [ ] **Step 4: Run review UI tests and complete frontend suite**

Run: `cd frontend && pnpm vitest run && pnpm build`

Expected: all frontend unit tests and the production build pass.

- [ ] **Step 5: Commit review UI**

```powershell
git add frontend/src
git commit -m "feat: add document review workspace"
```

### Task 12: Windows Launcher, Docker Deployment, E2E Verification, and Operator Guide

**Files:**
- Create: `docker/backend.Dockerfile`
- Create: `docker/frontend.Dockerfile`
- Create: `docker/nginx.conf`
- Create: `docker-compose.yml`
- Create: `.env.example`
- Create: `scripts/start-demo.ps1`
- Create: `scripts/stop-demo.ps1`
- Create: `frontend/playwright.config.ts`
- Create: `frontend/e2e/demo-flow.spec.ts`
- Create: `README.md`
- Modify: `.gitignore`

**Interfaces:**
- Produces: `powershell -ExecutionPolicy Bypass -File scripts/start-demo.ps1`
- Produces: `docker compose up --build`
- Produces: browser entry `http://localhost:8080`

- [ ] **Step 1: Write the failing Playwright standard-demo test**

```ts
test("offline demo completes all three business flows", async ({ page }) => {
  await page.goto("/");
  await expect(page.getByText("本地化大模型应用平台")).toBeVisible();
  await page.getByRole("link", { name: "坐席辅助" }).click();
  await page.getByRole("button", { name: "生成推荐话术" }).click();
  await expect(page.getByText(/已为您登记故障/)).toBeVisible();
  await page.getByRole("link", { name: "运维知识库" }).click();
  await page.getByLabel("运维问题").fill("核心交换机 VLAN 如何划分？");
  await page.getByRole("button", { name: "查询" }).click();
  await expect(page.getByRole("button", { name: "来源 1" })).toBeVisible();
  await page.getByRole("link", { name: "文档审核" }).click();
  await page.getByLabel("选择待审文件").setInputFiles("../backend/app/data/review_samples/technical-proposal.txt");
  await page.getByRole("button", { name: "开始审核" }).click();
  await expect(page.getByText("审核完成")).toBeVisible({ timeout: 30_000 });
});
```

- [ ] **Step 2: Add container images and Nginx API proxy**

The backend image runs Uvicorn on port 8000 and stores SQLite/uploads under `/data`. The frontend image builds Vite assets, serves them through Nginx on port 80, proxies `/api` to `backend:8000`, and falls back to `index.html` for SPA routes. Compose exposes only frontend port 8080 and mounts a named backend-data volume.

- [ ] **Step 3: Add Windows start and stop scripts**

`start-demo.ps1` resolves the repository root, checks Python and pnpm, installs missing project dependencies only after an explicit first-run message, starts Uvicorn and Vite with hidden child windows, waits for both health endpoints, writes PIDs to `data/runtime/processes.json`, and opens `http://localhost:5173`. `stop-demo.ps1` validates stored PIDs belong to the expected command lines before stopping them and removes only the PID file.

- [ ] **Step 4: Add operator documentation and environment template**

Document Windows startup, Docker startup, demo-mode story, real-provider variables `DEMO_OPENAI_BASE_URL`, `DEMO_OPENAI_MODEL`, `DEMO_OPENAI_API_KEY`, data reset steps, upload limits, disclaimers, and troubleshooting for ports, model timeouts, scanned PDFs, and interrupted review tasks.

- [ ] **Step 5: Run all automated checks**

Run:

```powershell
cd backend
python -m pytest -q
python -m ruff check app tests
cd ..\frontend
pnpm vitest run
pnpm build
pnpm exec playwright test
```

Expected: backend tests, lint, frontend tests, build, and the standard demo E2E test all pass.

- [ ] **Step 6: Verify both startup paths**

Run Windows path:

```powershell
powershell -ExecutionPolicy Bypass -File scripts/start-demo.ps1
Invoke-RestMethod http://localhost:8000/api/health
powershell -ExecutionPolicy Bypass -File scripts/stop-demo.ps1
```

Expected: health returns `status = ok`, the UI opens, and both owned processes stop.

Run Docker path:

```powershell
docker compose up --build -d
Invoke-RestMethod http://localhost:8080/api/health
docker compose down
```

Expected: proxied health returns `status = ok`, and Compose shuts down without deleting the named data volume.

- [ ] **Step 7: Perform visual QA at 1366×768**

Open each route at 1366×768 and verify: no horizontal page scroll; sidebar remains usable; call columns do not cover actions; source drawer fits the viewport; review filters and export stay reachable; all loading, empty, error, and completed states retain readable contrast.

- [ ] **Step 8: Commit packaging and verification**

```powershell
git add .env.example .gitignore docker docker-compose.yml scripts frontend/e2e frontend/playwright.config.ts README.md
git commit -m "feat: package and verify local LLM demo"
```

## Completion Gate

Before reporting completion, run the full commands in Task 12 Step 5, confirm Windows startup, confirm Docker startup when Docker is available, and inspect every route at 1366×768. If Docker is unavailable, report that single environmental limitation explicitly and do not claim Docker runtime verification.
