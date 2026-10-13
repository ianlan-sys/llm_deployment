# Local Model and Operations Knowledge Upgrade Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add an internal Ollama-hosted Qwen3 model, controlled operations-document ingestion, per-feature model routing, demo authentication, streaming cited knowledge answers, and real 24-hour model monitoring to the existing Web Demo.

**Architecture:** Keep the React/Nginx and FastAPI/SQLite application boundaries, add Ollama and a model-initializer to Docker Compose, and expose Ollama only on the Compose network. Add focused backend services for authentication, feature routing, inference admission/metrics, knowledge ingestion/indexing, and monitoring; the frontend consumes authenticated APIs and SSE without ever receiving model credentials or direct Ollama access.

**Tech Stack:** Python 3.11, FastAPI, SQLAlchemy 2, SQLite, httpx, psutil, NumPy/optional FAISS, React 19, TypeScript, TanStack Query, Vite, Vitest, Testing Library, Playwright, Docker Compose, Ollama, `qwen3:1.7b-q4_K_M`.

## Global Constraints

- The target server remains 2 vCPU, 8 GiB RAM, CPU-only; local inference concurrency is exactly 1, maximum queued requests is exactly 3, and queue-plus-inference timeout is 90 seconds.
- Ollama uses `qwen3:1.7b-q4_K_M`, is reachable only at `http://ollama:11434` inside Compose, and has no published host port.
- Default feature modes are `call_assistant=mock`, `knowledge=local`, and `document_review=mock`; changing one feature must not mutate another feature.
- Never silently fall back from `local` to `mock`; return a redacted Chinese error and expose an explicit manual mode change.
- Only `active` knowledge documents participate in retrieval; TXT, PDF, and DOCX uploads are limited to 20 MiB and must pass parse preview plus human approval before activation.
- Monitoring retains exactly the latest 24 hours, samples host-level CPU/memory/disk every 10 seconds, and never persists prompts, questions, answers, transcripts, document text, passwords, cookies, session tokens, or API keys.
- Authentication is enabled in the server Compose deployment, uses an 8-hour HttpOnly SameSite=Lax cookie, and limits one IP to 5 failed logins per 10 minutes.
- HTTP on port 8080 remains a short-term demo limitation; the UI must warn users not to enter production credentials, configuration, or sensitive documents.
- Preserve existing data and startup compatibility: old SQLite databases at commit `5d60782` must migrate in place without deleting or recreating existing tables.
- Browser and business APIs must never receive an Ollama credential, model-service secret, server root password, password hash, or session signing secret.
- Follow strict TDD for every task: observe a targeted RED, implement the minimum behavior, observe GREEN, run the stated regression command, then commit.

---

## File and Responsibility Map

### Backend additions

- `backend/app/migrations.py`: idempotent SQLite schema upgrades for existing databases.
- `backend/app/auth.py`: password verification, session creation/validation/revocation, login throttling, and origin checks.
- `backend/app/api/auth.py`: login, logout, and current-session endpoints.
- `backend/app/auth_middleware.py`: protect business APIs and attach the authenticated principal.
- `backend/app/providers/ollama.py`: Ollama version, tags, running-model, and OpenAI-compatible health adapter.
- `backend/app/providers/feature_router.py`: immutable per-feature provider snapshots and transactional publication.
- `backend/app/services/inference.py`: bounded local-inference admission, timeout/cancellation, and metric recording.
- `backend/app/services/knowledge_ingestion.py`: upload persistence, parse preview, review transitions, and activation.
- `backend/app/services/knowledge_index.py`: immutable active-knowledge snapshot and atomic rebuild.
- `backend/app/services/monitoring.py`: resource sampler, 24-hour retention, and aggregate calculations.
- `backend/app/api/knowledge_documents.py`: document list/upload/preview/approve/reject/archive APIs.
- `backend/app/api/monitoring.py`: live state and 24-hour metrics APIs.

### Backend modifications

- `backend/app/models.py`: auth, feature-mode, document metadata, request metric, and resource sample tables.
- `backend/app/schemas.py`: exact request/response/SSE schemas for the new APIs.
- `backend/app/config.py`: auth, Ollama, queue, and monitoring settings.
- `backend/app/main.py`: migrations, middleware, services, samplers, routers, and shutdown.
- `backend/app/providers/base.py`: streaming knowledge-answer interface.
- `backend/app/providers/mock.py`: deterministic implementation of the new interface.
- `backend/app/providers/openai_compatible.py`: usage-aware streaming and no-thinking request controls.
- `backend/app/api/calls.py`, `backend/app/api/knowledge.py`, `backend/app/api/reviews.py`: acquire the correct feature snapshot and inference admission.
- `backend/app/services/review_pipeline.py`: use the document-review feature snapshot and coordinator.
- `backend/app/retrieval/service.py`: consume an immutable active index snapshot.
- `backend/app/seed.py` and `backend/app/data/knowledge_sources.json`: synchronize about 30 fictional sources plus recommended questions.

### Frontend additions

- `frontend/src/auth/AuthProvider.tsx`: current-session query, login/logout mutations, and 401 handling.
- `frontend/src/auth/ProtectedLayout.tsx`: route guard and return-location preservation.
- `frontend/src/pages/LoginPage.tsx`: short-term demo login UI and HTTP warning.
- `frontend/src/pages/KnowledgeDocumentsPage.tsx`: upload, list, preview, approve/reject/archive, and retry UI.
- `frontend/src/pages/ModelMonitoringPage.tsx`: live status, queue, resource cards, and 24-hour charts.
- `frontend/src/components/MetricLineChart.tsx`: dependency-free accessible SVG chart.
- `frontend/src/api/sse.ts`: cancellable SSE POST parser shared by call and knowledge flows.

### Frontend modifications

- `frontend/src/api/client.ts`, `frontend/src/api/types.ts`: authenticated errors and all new contracts.
- `frontend/src/app/router.tsx`, `frontend/src/app/routes.tsx`, `frontend/src/components/AppShell.tsx`: login, protected routes, navigation, logout.
- `frontend/src/pages/KnowledgePage.tsx`: recommended questions, retrieval stage, local-generation stage, streaming answer, citations, and mode/error actions.
- `frontend/src/pages/SettingsPage.tsx`: independent feature-mode cards.
- `frontend/src/styles/global.css`, `frontend/src/styles/tokens.css`: login, ingestion, monitoring, chart, queue, and warning styles.

### Deployment and documentation

- `docker-compose.yml`: Ollama, model initialization, volumes, auth configuration, and backend dependency wiring.
- `.env.example`: non-secret server variable names and generation commands.
- `docker/nginx.conf`: keep API proxying and prevent direct Ollama exposure.
- `README.md`: setup, login generation, model initialization, operations, backup, rollback, and HTTP limitation.
- `scripts/generate-demo-auth.ps1`: generate a random demo password, scrypt hash, and session secret without persisting plaintext.

---

### Task 1: Add idempotent schema migrations and persistence models

**Files:**
- Create: `backend/app/migrations.py`
- Modify: `backend/app/models.py`
- Modify: `backend/app/main.py`
- Test: `backend/tests/test_migrations.py`

**Interfaces:**
- Produces: `run_schema_migrations(engine: Engine) -> None`.
- Produces models: `AuthSession`, `LoginAttempt`, `FeatureProviderSetting`, `ModelRequestMetric`, `ResourceSample`.
- Extends `KnowledgeSource` with nullable legacy-safe metadata and review timestamps.
- Later tasks may assume every new table and column exists after `run_schema_migrations()`.

- [ ] **Step 1: Write the failing old-database migration tests**

```python
def test_migrates_pre_upgrade_database_without_losing_sources(tmp_path):
    url = f"sqlite:///{tmp_path / 'legacy.db'}"
    engine = create_engine(url)
    with engine.begin() as connection:
        connection.exec_driver_sql(
            "CREATE TABLE knowledge_sources (id VARCHAR(36) PRIMARY KEY, title VARCHAR(200) NOT NULL, "
            "source_type VARCHAR(50) NOT NULL, scope VARCHAR(150) NOT NULL, content TEXT NOT NULL, "
            "status VARCHAR(30) NOT NULL, updated_at DATETIME NOT NULL)"
        )
        connection.exec_driver_sql(
            "INSERT INTO knowledge_sources VALUES "
            "('legacy-source','旧资料','operation_manual','全局','保留内容','active','2026-01-01')"
        )

    run_schema_migrations(engine)
    run_schema_migrations(engine)

    columns = {row[1] for row in engine.connect().exec_driver_sql("PRAGMA table_info(knowledge_sources)")}
    assert {"category", "version", "effective_date", "file_path", "media_type", "file_size", "reviewed_at", "reviewed_by"} <= columns
    with Session(engine) as session:
        assert session.get(KnowledgeSource, "legacy-source").content == "保留内容"
```

- [ ] **Step 2: Run the migration test and observe RED**

Run: `cd backend && py -3.11 -m pytest tests/test_migrations.py -v`
Expected: FAIL because `app.migrations` and the new columns/tables do not exist.

- [ ] **Step 3: Add models and an idempotent SQLite migration runner**

```python
KNOWLEDGE_SOURCE_COLUMNS = {
    "category": "VARCHAR(100)",
    "version": "VARCHAR(50)",
    "effective_date": "DATE",
    "file_path": "VARCHAR(500)",
    "media_type": "VARCHAR(150)",
    "file_size": "INTEGER",
    "content_summary": "TEXT",
    "reviewed_at": "DATETIME",
    "reviewed_by": "VARCHAR(100)",
}


def run_schema_migrations(engine: Engine) -> None:
    Base.metadata.create_all(engine)
    if engine.dialect.name != "sqlite":
        return
    with engine.begin() as connection:
        existing = {
            row[1]
            for row in connection.exec_driver_sql("PRAGMA table_info(knowledge_sources)")
        }
        for name, sql_type in KNOWLEDGE_SOURCE_COLUMNS.items():
            if name not in existing:
                connection.exec_driver_sql(
                    f"ALTER TABLE knowledge_sources ADD COLUMN {name} {sql_type}"
                )
```

Define exact columns for request metrics: feature, mode, model, request ID, operation, queue wait, TTFT, elapsed, input/output tokens, result status, and created time. Define resource samples with CPU, memory, disk, Ollama availability, model-loaded flag, running count, and queued count. Store only hashes for session tokens.

- [ ] **Step 4: Call migrations before seeding and run GREEN**

Replace `Base.metadata.create_all(engine)` in `main.py` with `run_schema_migrations(engine)`.
Run: `cd backend && py -3.11 -m pytest tests/test_migrations.py tests/test_seed.py tests/test_health.py -v`
Expected: all tests PASS and running migrations twice changes no data.

- [ ] **Step 5: Run model/style regression and commit**

Run: `cd backend && py -3.11 -m ruff check app/models.py app/migrations.py app/main.py tests/test_migrations.py`
Expected: `All checks passed!`

```bash
git add backend/app/models.py backend/app/migrations.py backend/app/main.py backend/tests/test_migrations.py
git commit -m "feat: add compatible schema migrations"
```

---

### Task 2: Implement backend demo authentication and API protection

**Files:**
- Create: `backend/app/auth.py`
- Create: `backend/app/auth_middleware.py`
- Create: `backend/app/api/auth.py`
- Modify: `backend/app/config.py`
- Modify: `backend/app/main.py`
- Modify: `backend/app/schemas.py`
- Modify: `backend/app/api/dashboard.py`
- Test: `backend/tests/test_auth_api.py`
- Test: `backend/tests/conftest.py`

**Interfaces:**
- Produces: `AuthService.login(username, password, client_ip) -> LoginResult`.
- Produces: `AuthService.authenticate(raw_token) -> AuthPrincipal | None` and `logout(raw_token) -> None`.
- Produces endpoints: `POST /api/auth/login`, `POST /api/auth/logout`, `GET /api/auth/session`.
- Produces request state: `request.state.principal.username` for protected APIs.

- [ ] **Step 1: Write RED tests for login, cookie, throttling, and protected APIs**

```python
def test_business_api_requires_session_and_login_sets_http_only_cookie(auth_client):
    assert auth_client.get("/api/dashboard").status_code == 401
    response = auth_client.post(
        "/api/auth/login", json={"username": "demo", "password": "correct horse"}
    )
    assert response.status_code == 200
    cookie = response.headers["set-cookie"]
    assert "HttpOnly" in cookie
    assert "SameSite=lax" in cookie
    assert "Max-Age=28800" in cookie
    assert auth_client.get("/api/dashboard").status_code == 200


def test_sixth_failed_login_is_rate_limited(auth_client):
    for _ in range(5):
        assert auth_client.post(
            "/api/auth/login", json={"username": "demo", "password": "wrong"}
        ).status_code == 401
    assert auth_client.post(
        "/api/auth/login", json={"username": "demo", "password": "wrong"}
    ).status_code == 429
```

Build the password hash in the fixture with `encode_scrypt_hash("correct horse")`; never put the plaintext password in application defaults.

- [ ] **Step 2: Run auth tests and observe RED**

Run: `cd backend && py -3.11 -m pytest tests/test_auth_api.py -v`
Expected: FAIL because auth settings, routes, middleware, and service do not exist.

- [ ] **Step 3: Implement scrypt verification and server-side sessions**

```python
def encode_scrypt_hash(password: str, salt: bytes | None = None) -> str:
    actual_salt = salt or secrets.token_bytes(16)
    digest = hashlib.scrypt(
        password.encode(), salt=actual_salt, n=2**14, r=8, p=1, dklen=32
    )
    return f"scrypt$16384$8$1${actual_salt.hex()}${digest.hex()}"


def verify_scrypt_hash(password: str, encoded: str) -> bool:
    algorithm, n, r, p, salt, expected = encoded.split("$")
    if algorithm != "scrypt":
        return False
    actual = hashlib.scrypt(
        password.encode(), salt=bytes.fromhex(salt), n=int(n), r=int(r), p=int(p), dklen=32
    )
    return hmac.compare_digest(actual, bytes.fromhex(expected))
```

Generate a 32-byte random raw session token, store only `sha256(token)` in `AuthSession`, and set raw token cookie `demo_session`. Delete expired sessions during authentication. Count `LoginAttempt` rows within 10 minutes by normalized client IP; the fifth failure is stored, and the next attempt returns 429.

- [ ] **Step 4: Add middleware exemptions and mutation origin validation**

Exempt only `/api/health`, `/api/auth/login`, and static non-API paths. Return JSON `{"detail":"请先登录"}` for unauthenticated business APIs. For POST/PUT/PATCH/DELETE authenticated requests, require the `Origin` host to equal the request host when an Origin header is present. Keep auth disabled only when `DEMO_AUTH_REQUIRED=false`; Compose will set it true.

- [ ] **Step 5: Run auth and audit GREEN**

Run: `cd backend && py -3.11 -m pytest tests/test_auth_api.py tests/test_dashboard_api.py -v`
Expected: PASS; audit summaries never contain usernames, passwords, raw cookies, token hashes, or session secrets.

- [ ] **Step 6: Run backend regression and commit**

Run: `cd backend && py -3.11 -m pytest -q && py -3.11 -m ruff check app tests`
Expected: existing tests remain green with `auth_required=False` test defaults; new auth fixtures explicitly enable auth.

```bash
git add backend/app/auth.py backend/app/auth_middleware.py backend/app/api/auth.py backend/app/config.py backend/app/main.py backend/app/schemas.py backend/app/api/dashboard.py backend/tests/test_auth_api.py backend/tests/conftest.py
git commit -m "feat: protect demo with server-side sessions"
```

---

### Task 3: Add the login page and protected frontend shell

**Files:**
- Create: `frontend/src/auth/AuthProvider.tsx`
- Create: `frontend/src/auth/ProtectedLayout.tsx`
- Create: `frontend/src/pages/LoginPage.tsx`
- Modify: `frontend/src/api/client.ts`
- Modify: `frontend/src/api/types.ts`
- Modify: `frontend/src/app/App.tsx`
- Modify: `frontend/src/app/router.tsx`
- Modify: `frontend/src/app/routes.tsx`
- Modify: `frontend/src/components/AppShell.tsx`
- Modify: `frontend/src/styles/global.css`
- Test: `frontend/src/test/AuthFlow.test.tsx`
- Test: `frontend/src/test/router.test.tsx`

**Interfaces:**
- Consumes: `/api/auth/login`, `/api/auth/logout`, `/api/auth/session` from Task 2.
- Produces: `useAuth(): AuthContextValue` with `principal`, `isLoading`, `login()`, and `logout()`.
- Produces: `/login` public route and a protected shell for every business route.

- [ ] **Step 1: Write failing protected-route tests**

```tsx
it('redirects an unauthenticated deep link to login and returns after success', async () => {
  mockSessionOnce(401)
  mockLoginSuccess({ username: 'demo', expires_at: '2026-07-22T18:00:00Z' })
  renderRouterAt('/knowledge')
  expect(await screen.findByRole('heading', { name: '演示系统登录' })).toBeVisible()
  await userEvent.type(screen.getByLabelText('账号'), 'demo')
  await userEvent.type(screen.getByLabelText('密码'), 'correct horse')
  await userEvent.click(screen.getByRole('button', { name: '登录' }))
  expect(await screen.findByRole('heading', { name: '运维知识库' })).toBeVisible()
})
```

- [ ] **Step 2: Run the frontend RED**

Run: `cd frontend && corepack pnpm vitest run src/test/AuthFlow.test.tsx src/test/router.test.tsx`
Expected: FAIL because the login route and auth provider do not exist.

- [ ] **Step 3: Implement the provider and protected layout**

```tsx
export function ProtectedLayout() {
  const auth = useAuth()
  const location = useLocation()
  if (auth.isLoading) return <section className="page-state">正在验证登录状态…</section>
  if (!auth.principal) {
    return <Navigate to="/login" replace state={{ from: location.pathname }} />
  }
  return <ShellLayout />
}
```

The login mutation invalidates `['auth-session']`; the logout mutation clears that query and navigates to `/login`. Add `credentials: 'same-origin'` to every API and SSE fetch. A 401 must reject as `ApiError(401, '登录状态已失效')`; route protection, not localStorage, determines navigation.

- [ ] **Step 4: Implement the login UI and shell logout**

Render the fixed warning: `当前为 HTTP 短期演示环境，请勿输入真实账号、密码、生产配置或敏感文档。` Disable the submit button while pending, preserve the account field on failure, clear only the password field, and add a visible logout button to `AppShell`.

- [ ] **Step 5: Run frontend GREEN and full regression**

Run: `cd frontend && corepack pnpm vitest run src/test/AuthFlow.test.tsx src/test/router.test.tsx src/test/AppShell.test.tsx && corepack pnpm test && corepack pnpm build && corepack pnpm lint`
Expected: all tests, TypeScript build, and lint PASS.

- [ ] **Step 6: Commit**

```bash
git add frontend/src/auth frontend/src/pages/LoginPage.tsx frontend/src/api frontend/src/app frontend/src/components/AppShell.tsx frontend/src/styles/global.css frontend/src/test/AuthFlow.test.tsx frontend/src/test/router.test.tsx
git commit -m "feat: add protected demo login flow"
```

---

### Task 4: Add Ollama Compose services and a health client

**Files:**
- Create: `backend/app/providers/ollama.py`
- Modify: `docker-compose.yml`
- Modify: `.env.example`
- Modify: `backend/app/config.py`
- Modify: `backend/app/main.py`
- Modify: `backend/pyproject.toml`
- Test: `backend/tests/test_ollama.py`
- Test: `backend/tests/test_final_static.py`

**Interfaces:**
- Produces: `OllamaClient.health() -> OllamaHealth`, `tags() -> list[OllamaModel]`, and `running_models() -> list[OllamaRunningModel]`.
- Produces settings: `ollama_base_url`, `ollama_model`, and `ollama_timeout_seconds`.
- Compose guarantees `model-init` exits zero only after the exact model tag is present.

- [ ] **Step 1: Write RED tests for Ollama response parsing and Compose isolation**

```python
def test_ollama_health_requires_exact_model_tag():
    transport = httpx.MockTransport(lambda request: httpx.Response(
        200,
        json={"models": [{"name": "qwen3:1.7b-q4_K_M", "size": 1_400_000_000}]},
    ))
    client = OllamaClient("http://ollama:11434", "qwen3:1.7b-q4_K_M", client=httpx.Client(transport=transport))
    assert client.model_status().installed is True


def test_ollama_has_no_published_port_in_compose():
    compose = yaml.safe_load((ROOT / "docker-compose.yml").read_text())
    assert "ports" not in compose["services"]["ollama"]
    assert compose["services"]["model-init"]["restart"] == "no"
```

- [ ] **Step 2: Run RED**

Run: `cd backend && py -3.11 -m pytest tests/test_ollama.py tests/test_final_static.py -v`
Expected: FAIL because `OllamaClient` and Compose services are absent.

- [ ] **Step 3: Implement the strict Ollama client**

```python
@dataclass(frozen=True)
class OllamaHealth:
    available: bool
    version: str | None
    model: str
    installed: bool
    loaded: bool
    size_bytes: int | None


class OllamaClient:
    def model_status(self) -> OllamaHealth:
        version = self._get("/api/version")["version"]
        tags = self._get("/api/tags").get("models", [])
        running = self._get("/api/ps").get("models", [])
        installed = next((item for item in tags if item.get("name") == self.model), None)
        return OllamaHealth(True, version, self.model, installed is not None,
                            any(item.get("name") == self.model for item in running),
                            installed.get("size") if installed else None)
```

Convert connection, timeout, invalid JSON, and missing-key failures to `ProviderUnavailable` without including upstream bodies.

- [ ] **Step 4: Add Compose services and persistent volume**

```yaml
  ollama:
    image: ollama/ollama:0.32.1
    volumes:
      - ollama-data:/root/.ollama
    healthcheck:
      test: ["CMD", "ollama", "list"]
      interval: 5s
      timeout: 4s
      retries: 24
    restart: unless-stopped

  model-init:
    image: ollama/ollama:0.32.1
    environment:
      OLLAMA_HOST: http://ollama:11434
    depends_on:
      ollama:
        condition: service_healthy
    command: ["pull", "qwen3:1.7b-q4_K_M"]
    restart: "no"
```

Add `ollama-data:` to volumes. Add backend environment `DEMO_OLLAMA_BASE_URL=http://ollama:11434`, `DEMO_OLLAMA_MODEL=qwen3:1.7b-q4_K_M`, and no browser-facing Ollama variable.

- [ ] **Step 5: Run GREEN and static Compose validation**

Run: `cd backend && py -3.11 -m pytest tests/test_ollama.py tests/test_final_static.py -v && py -3.11 -m ruff check app/providers/ollama.py tests/test_ollama.py`
Run: `docker compose config --quiet`
Expected: tests and Ruff PASS; Compose configuration is valid without starting containers.

- [ ] **Step 6: Commit**

```bash
git add backend/app/providers/ollama.py backend/app/config.py backend/app/main.py backend/pyproject.toml backend/tests/test_ollama.py backend/tests/test_final_static.py docker-compose.yml .env.example
git commit -m "feat: add private Ollama model service"
```

---

### Task 5: Replace the global provider with per-feature routing

**Files:**
- Create: `backend/app/providers/feature_router.py`
- Modify: `backend/app/providers/registry.py`
- Modify: `backend/app/api/settings.py`
- Modify: `backend/app/schemas.py`
- Modify: `backend/app/main.py`
- Modify: `backend/app/api/dashboard.py`
- Test: `backend/tests/test_feature_router.py`
- Test: `backend/tests/test_settings_api.py`
- Test: `backend/tests/test_health.py`

**Interfaces:**
- Consumes: `OllamaClient` from Task 4 and `FeatureProviderSetting` from Task 1.
- Produces: `FeatureKey = Literal['call_assistant','knowledge','document_review']`.
- Produces: `FeatureProviderRouter.snapshot(feature) -> FeatureProviderSnapshot`.
- Produces endpoints: `GET /api/settings/features`, `PUT /api/settings/features/{feature}`, and `POST /api/settings/features/{feature}/test`.

- [ ] **Step 1: Write RED tests for defaults, isolation, rollback, and legacy audits**

```python
def test_feature_defaults_and_independent_publication(db_session, ollama_client):
    router = FeatureProviderRouter(db_session.bind, ollama_client)
    router.load(db_session)
    assert router.snapshot("call_assistant").mode == "mock"
    assert router.snapshot("knowledge").mode == "local"
    assert router.snapshot("document_review").mode == "mock"

    router.configure_and_persist(db_session, "call_assistant", "local")
    assert router.snapshot("call_assistant").mode == "local"
    assert router.snapshot("document_review").mode == "mock"
```

Add a commit-failure test proving the immutable snapshot remains unchanged. Add a concurrency test that repeatedly reads feature, mode, model, and provider identity from one snapshot while another thread toggles the same feature.

- [ ] **Step 2: Run RED**

Run: `cd backend && py -3.11 -m pytest tests/test_feature_router.py tests/test_settings_api.py tests/test_health.py -v`
Expected: FAIL because the feature router and endpoints are absent.

- [ ] **Step 3: Implement immutable snapshots and exact defaults**

```python
FeatureKey = Literal["call_assistant", "knowledge", "document_review"]
FeatureMode = Literal["mock", "local"]


@dataclass(frozen=True)
class FeatureProviderSnapshot:
    feature: FeatureKey
    mode: FeatureMode
    model: str | None
    provider: AIProvider


DEFAULT_MODES: dict[FeatureKey, FeatureMode] = {
    "call_assistant": "mock",
    "knowledge": "local",
    "document_review": "mock",
}
```

Build all desired snapshots before taking the router lock. In `configure_and_persist`, validate `local` with Ollama first, commit `FeatureProviderSetting`, then publish under the lock. Never mutate the router if validation or commit fails.

- [ ] **Step 4: Implement settings APIs and backward-safe audit rendering**

Return all three feature settings in stable order. A local-mode update returns 503 if the exact model is not installed. Extend audit mode response to accept historical `demo`/`real` and new `mock`/`local`; new events use only `mock` or `local`.

- [ ] **Step 5: Run GREEN and backend regression**

Run: `cd backend && py -3.11 -m pytest tests/test_feature_router.py tests/test_settings_api.py tests/test_health.py tests/test_dashboard_api.py -v && py -3.11 -m pytest -q`
Expected: all tests PASS; one feature change never alters another.

- [ ] **Step 6: Commit**

```bash
git add backend/app/providers/feature_router.py backend/app/providers/registry.py backend/app/api/settings.py backend/app/schemas.py backend/app/main.py backend/app/api/dashboard.py backend/tests/test_feature_router.py backend/tests/test_settings_api.py backend/tests/test_health.py
git commit -m "feat: route models independently by feature"
```

---

### Task 6: Add bounded inference admission and content-free request metrics

**Files:**
- Create: `backend/app/services/inference.py`
- Modify: `backend/app/providers/base.py`
- Modify: `backend/app/providers/mock.py`
- Modify: `backend/app/providers/openai_compatible.py`
- Modify: `backend/app/main.py`
- Test: `backend/tests/test_inference.py`
- Test: `backend/tests/test_providers.py`

**Interfaces:**
- Consumes: `ModelRequestMetric` from Task 1 and feature snapshots from Task 5.
- Produces: `InferenceCoordinator.execute(feature, operation, snapshot, callback)`.
- Produces: `InferenceCoordinator.stream(feature, operation, snapshot, iterator_factory)`.
- Produces: `InferenceQueueFull`, `InferenceTimedOut`, and `InferenceStats`.

- [ ] **Step 1: Write RED concurrency, timeout, cancellation, and redaction tests**

```python
def test_local_gate_allows_one_running_and_three_waiting(session_factory):
    coordinator = InferenceCoordinator(session_factory, concurrency=1, max_queue=3, timeout_seconds=0.2)
    blocker = Event()
    running = Thread(target=lambda: coordinator.execute(
        "knowledge", "answer", local_snapshot(), lambda: blocker.wait(1)
    ))
    running.start()
    wait_until(lambda: coordinator.live_state().running == 1)
    queued = [submit_waiting(coordinator) for _ in range(3)]
    with pytest.raises(InferenceQueueFull):
        coordinator.execute("knowledge", "answer", local_snapshot(), lambda: None)
    blocker.set()
    running.join()
    for future in queued:
        future.result()
```

Persist a metric and assert serialized database rows contain none of a sentinel question, answer, document text, or API key.

- [ ] **Step 2: Run RED**

Run: `cd backend && py -3.11 -m pytest tests/test_inference.py tests/test_providers.py -v`
Expected: FAIL because admission, stats, and usage-aware providers do not exist.

- [ ] **Step 3: Implement a cross-thread gate and metric lifecycle**

```python
@dataclass(frozen=True)
class InferenceStats:
    request_id: str
    queue_wait_ms: int
    ttft_ms: int | None
    elapsed_ms: int
    input_tokens: int | None
    output_tokens: int | None
    result_status: str
```

Protect running and queued counters with `threading.Condition`. Reject before incrementing when queued count is 3. Use a monotonic deadline covering queue wait plus execution. Release admission in `finally` for success, provider error, timeout, generator close, and cancellation. Write metrics through a fresh session from `session_factory`; metric-write failure must not mask the business result.

- [ ] **Step 4: Capture usage and disable thinking for Qwen3**

Add `reasoning_effort: "none"` to local OpenAI-compatible payloads and parse optional `usage.prompt_tokens` and `usage.completion_tokens`. For streaming, request `stream_options: {"include_usage": true}` and capture the final usage frame without emitting it as answer text. Mock returns deterministic token counts as `None`.

- [ ] **Step 5: Run GREEN and race regression**

Run: `cd backend && py -3.11 -m pytest tests/test_inference.py tests/test_providers.py -v --maxfail=1`
Expected: PASS with running=0 and queued=0 after every terminal path.

- [ ] **Step 6: Commit**

```bash
git add backend/app/services/inference.py backend/app/providers/base.py backend/app/providers/mock.py backend/app/providers/openai_compatible.py backend/app/main.py backend/tests/test_inference.py backend/tests/test_providers.py
git commit -m "feat: bound and measure local inference"
```

---

### Task 7: Build the reviewed knowledge-document lifecycle

**Files:**
- Create: `backend/app/services/knowledge_ingestion.py`
- Create: `backend/app/api/knowledge_documents.py`
- Create: `backend/app/data/recommended_questions.json`
- Modify: `backend/app/data/knowledge_sources.json`
- Modify: `backend/app/seed.py`
- Modify: `backend/app/models.py`
- Modify: `backend/app/schemas.py`
- Modify: `backend/app/main.py`
- Test: `backend/tests/test_knowledge_documents_api.py`
- Test: `backend/tests/test_seed.py`

**Interfaces:**
- Reuses: `parse_document(path, media_type)` from `review_parser.py`.
- Produces endpoints: list, create upload, detail/preview, approve, reject, archive, and retry.
- Produces `KnowledgeIngestionService.parse(document_id)` and `approve(document_id, reviewer)`; this task uses the existing database-backed retrieval path, and Task 8 upgrades activation to an atomic in-memory snapshot.
- Produces about 30 active seeded documents and exactly 10 recommended questions.

- [ ] **Step 1: Write RED lifecycle and seed tests**

```python
def test_uploaded_document_requires_approval_before_retrieval(auth_client, tmp_path):
    response = auth_client.post(
        "/api/knowledge-documents",
        files={"file": ("runbook.txt", b"Database recovery uses checkpoint R-42.", "text/plain")},
        data={"title": "数据库恢复手册", "category": "备份与恢复", "scope": "全局", "version": "1.0"},
    )
    assert response.status_code == 202
    document_id = response.json()["id"]
    wait_for_document(auth_client, document_id, "pending_review")
    assert query_sources(auth_client, "checkpoint R-42") == []
    approved = auth_client.post(f"/api/knowledge-documents/{document_id}/approve")
    assert approved.json()["status"] == "active"
    assert query_sources(auth_client, "checkpoint R-42")[0]["source_id"] == document_id
```

Assert six exact categories, 28–32 sources, exactly 10 questions, and only fictional/reserved values.

- [ ] **Step 2: Run RED**

Run: `cd backend && py -3.11 -m pytest tests/test_knowledge_documents_api.py tests/test_seed.py -v`
Expected: FAIL because lifecycle APIs and expanded fixtures are absent.

- [ ] **Step 3: Implement safe upload and parsing transitions**

Validate extension, media type, and actual byte count before writing to `runtime_dir/knowledge_uploads/{uuid}{suffix}`. Use an executor with at most 2 parser workers. Update states with conditional SQL transitions so stale parse attempts cannot overwrite rejection/archive. On parse success, replace preview chunks transactionally and set `pending_review`; on failure store a fixed Chinese error summary no longer than 500 characters.

```python
ALLOWED_TRANSITIONS = {
    "uploaded": {"parsing"},
    "parsing": {"pending_review", "parse_failed"},
    "pending_review": {"indexing", "rejected"},
    "indexing": {"active", "index_failed"},
    "parse_failed": {"parsing", "archived"},
    "index_failed": {"indexing", "archived"},
    "active": {"archived"},
    "rejected": {"archived"},
}
```

- [ ] **Step 4: Implement review actions and redacted audits**

Use `request.state.principal.username` as `reviewed_by`. In this incremental task, approval validates that parsed chunks exist, transitions `pending_review -> indexing -> active` in one transaction, and relies on the existing database-backed `RetrievalService`, which reads active rows on every query. Keep `index_failed` reserved for Task 8's snapshot upgrade. Audit fixed summaries only: `Knowledge document uploaded`, `parsed`, `approved`, `rejected`, `archived`, or `failed`, with document ID and status but no filename/content.

- [ ] **Step 5: Expand fixtures and run GREEN**

Create six categories with about five sources each and ten recommended questions. Keep stable UUID derivation in `seed.py`, synchronize existing IDs without duplication, and set bundled sources directly to `active` with reviewer `system-seed`.

Run: `cd backend && py -3.11 -m pytest tests/test_knowledge_documents_api.py tests/test_seed.py tests/test_retrieval.py -v`
Expected: PASS; unapproved sentinel text is not retrievable and approved text is retrievable.

- [ ] **Step 6: Commit**

```bash
git add backend/app/services/knowledge_ingestion.py backend/app/api/knowledge_documents.py backend/app/data/knowledge_sources.json backend/app/data/recommended_questions.json backend/app/seed.py backend/app/models.py backend/app/schemas.py backend/app/main.py backend/tests/test_knowledge_documents_api.py backend/tests/test_seed.py
git commit -m "feat: add reviewed knowledge ingestion"
```

---

### Task 8: Add atomic active indexing and streaming cited RAG

**Files:**
- Create: `backend/app/services/knowledge_index.py`
- Modify: `backend/app/retrieval/service.py`
- Modify: `backend/app/services/knowledge.py`
- Modify: `backend/app/services/knowledge_ingestion.py`
- Modify: `backend/app/providers/base.py`
- Modify: `backend/app/providers/mock.py`
- Modify: `backend/app/providers/openai_compatible.py`
- Modify: `backend/app/api/knowledge.py`
- Modify: `backend/app/schemas.py`
- Modify: `backend/app/main.py`
- Test: `backend/tests/test_knowledge_index.py`
- Test: `backend/tests/test_knowledge_stream_api.py`
- Modify test: `backend/tests/test_knowledge_documents_api.py`

**Interfaces:**
- Consumes: feature router from Task 5 and inference coordinator from Task 6.
- Produces: `KnowledgeIndexService.rebuild(session) -> KnowledgeIndexSnapshot` and `search(query, scope, limit=4)`.
- Produces: `POST /api/knowledge/query/stream` SSE events `retrieval`, `meta`, `delta`, `done`, and `error`.
- Produces provider method `stream_answer_with_context(question, chunks) -> Iterator[str]`.

- [ ] **Step 1: Write RED tests for atomic activation, SSE stages, cancellation, and no-evidence**

```python
def test_stream_returns_retrieval_before_local_generation(auth_client):
    frames = parse_sse(auth_client.stream(
        "POST", "/api/knowledge/query/stream", json={"question": "VLAN 如何配置？", "scope": None}
    ))
    assert [frame.event for frame in frames][:2] == ["retrieval", "meta"]
    assert frames[0].data["citations"]
    assert frames[1].data == {"mode": "local", "model": "qwen3:1.7b-q4_K_M"}
    assert "".join(frame.data["content"] for frame in frames if frame.event == "delta")
    assert frames[-1].event == "done"


def test_no_evidence_does_not_enter_inference_gate(auth_client, coordinator):
    frames = query_stream(auth_client, "不存在的量子网络参数")
    assert frames[-1].data["status"] == "no_evidence"
    assert coordinator.recorded_requests == 0
```

- [ ] **Step 2: Run RED**

Run: `cd backend && py -3.11 -m pytest tests/test_knowledge_index.py tests/test_knowledge_stream_api.py -v`
Expected: FAIL because the index service, provider method, and SSE route are absent.

- [ ] **Step 3: Implement immutable index snapshots**

```python
@dataclass(frozen=True)
class KnowledgeIndexSnapshot:
    records: tuple[IndexedChunk, ...]
    vectors: np.ndarray
    engine_name: str
    built_at: datetime


class KnowledgeIndexService:
    def rebuild(self, session: Session) -> KnowledgeIndexSnapshot:
        candidate = self._build_candidate(session, status="active")
        with self._lock:
            self._snapshot = candidate
        return candidate
```

Build candidates outside the lock. Search one acquired snapshot so source metadata and vectors cannot diverge during approval. Rebuild at startup after seeding and after archive. Replace Task 7's approval transition with `build_candidate(session, include_document_id=document_id)` followed by a single critical section that swaps the snapshot and commits `active`; if candidate construction fails, preserve the previous snapshot and persist `index_failed`. Add a regression test proving the previously active snapshot remains searchable after a failed approval.

- [ ] **Step 4: Stream answers while keeping citations deterministic**

Use the top four validated retrieval results as citations; the model streams answer text only and cannot invent citation identifiers. Prompt Qwen3 to use only supplied chunks and say no evidence when context is insufficient. Emit citations before generation, pass the iterator through `InferenceCoordinator.stream`, and close it in every cancellation/error path. Keep the existing non-stream endpoint as a compatibility wrapper that consumes the same service.

- [ ] **Step 5: Run GREEN and full knowledge regression**

Run: `cd backend && py -3.11 -m pytest tests/test_knowledge_index.py tests/test_knowledge_stream_api.py tests/test_knowledge_api.py tests/test_retrieval.py -v`
Expected: PASS; no event or metric contains the full question or answer.

- [ ] **Step 6: Commit**

```bash
git add backend/app/services/knowledge_index.py backend/app/retrieval/service.py backend/app/services/knowledge.py backend/app/services/knowledge_ingestion.py backend/app/providers/base.py backend/app/providers/mock.py backend/app/providers/openai_compatible.py backend/app/api/knowledge.py backend/app/schemas.py backend/app/main.py backend/tests/test_knowledge_index.py backend/tests/test_knowledge_stream_api.py backend/tests/test_knowledge_documents_api.py
git commit -m "feat: stream cited local knowledge answers"
```

---

### Task 9: Build the knowledge-document and streaming-answer frontend

**Files:**
- Create: `frontend/src/api/sse.ts`
- Create: `frontend/src/pages/KnowledgeDocumentsPage.tsx`
- Modify: `frontend/src/pages/KnowledgePage.tsx`
- Modify: `frontend/src/api/client.ts`
- Modify: `frontend/src/api/types.ts`
- Modify: `frontend/src/app/router.tsx`
- Modify: `frontend/src/components/AppShell.tsx`
- Modify: `frontend/src/styles/global.css`
- Test: `frontend/src/test/KnowledgeDocumentsPage.test.tsx`
- Test: `frontend/src/test/KnowledgePage.test.tsx`

**Interfaces:**
- Consumes Task 7 document APIs and Task 8 knowledge SSE events.
- Produces `/knowledge/documents` protected route.
- Produces a cancellable knowledge query with visible retrieval, generation, completion, and error stages.

- [ ] **Step 1: Write RED tests for upload review and streamed answer stages**

```tsx
it('previews and approves an uploaded document before showing it active', async () => {
  mockDocumentLifecycle()
  renderRoute('/knowledge/documents')
  await userEvent.upload(screen.getByLabelText('选择运维资料'), new File(['恢复步骤'], '恢复手册.txt', { type: 'text/plain' }))
  await userEvent.click(screen.getByRole('button', { name: '上传并解析' }))
  expect(await screen.findByText('待审核')).toBeVisible()
  expect(screen.getByText('恢复步骤')).toBeVisible()
  await userEvent.click(screen.getByRole('button', { name: '审核通过' }))
  await userEvent.click(screen.getByRole('button', { name: '确认入库' }))
  expect(await screen.findByText('已生效')).toBeVisible()
})
```

Add a knowledge-page test that receives `retrieval`, `meta`, two `delta`, and `done` frames and asserts the local model badge, concatenated text, citation drawer, and elapsed time.

- [ ] **Step 2: Run RED**

Run: `cd frontend && corepack pnpm vitest run src/test/KnowledgeDocumentsPage.test.tsx src/test/KnowledgePage.test.tsx`
Expected: FAIL because management route and knowledge SSE handling are absent.

- [ ] **Step 3: Extract a reusable cancellable SSE client**

```ts
export async function postSse<T>(
  path: string,
  body: Record<string, unknown>,
  onEvent: (frame: SseEvent<T>) => void,
  signal: AbortSignal,
) {
  const response = await fetch(path, {
    method: 'POST', credentials: 'same-origin', signal,
    headers: { 'Content-Type': 'application/json', Accept: 'text/event-stream' },
    body: JSON.stringify(body),
  })
  if (!response.ok || !response.body) throw await ApiError.fromResponse(response)
  await readSseFrames(response.body, onEvent, signal)
}
```

Move call-assistant streaming to this helper so there is one parser and one cancellation behavior.

- [ ] **Step 4: Implement document management and knowledge stage UI**

Poll parsing/indexing documents every second and stop polling terminal states. Preserve selected metadata and preview when approve/reject fails. Require a confirmation modal for approve, reject, and archive. On the knowledge page, keep the question on failure, show retrieved titles before answer tokens, and expose Retry plus Switch This Feature to Mock actions.

- [ ] **Step 5: Run GREEN, visual unit regression, and build**

Run: `cd frontend && corepack pnpm vitest run src/test/KnowledgeDocumentsPage.test.tsx src/test/KnowledgePage.test.tsx src/test/CallAssistantPage.test.tsx && corepack pnpm test && corepack pnpm build && corepack pnpm lint`
Expected: all tests/build/lint PASS.

- [ ] **Step 6: Commit**

```bash
git add frontend/src/api frontend/src/pages/KnowledgeDocumentsPage.tsx frontend/src/pages/KnowledgePage.tsx frontend/src/app/router.tsx frontend/src/components/AppShell.tsx frontend/src/styles/global.css frontend/src/test/KnowledgeDocumentsPage.test.tsx frontend/src/test/KnowledgePage.test.tsx frontend/src/test/CallAssistantPage.test.tsx
git commit -m "feat: add reviewed knowledge workspace"
```

---

### Task 10: Replace global settings with independent feature-mode controls

**Files:**
- Modify: `frontend/src/pages/SettingsPage.tsx`
- Modify: `frontend/src/api/types.ts`
- Modify: `frontend/src/styles/global.css`
- Test: `frontend/src/test/SettingsPage.test.tsx`
- Test: `backend/tests/test_settings_api.py`

**Interfaces:**
- Consumes Task 5 feature settings endpoints.
- Produces one mode card per feature with persisted active state, unsaved candidate state, connection test, and explicit save.

- [ ] **Step 1: Write RED tests for independent mode state**

```tsx
it('switches knowledge to mock without changing call or review', async () => {
  mockFeatureSettings([
    { feature: 'call_assistant', mode: 'mock' },
    { feature: 'knowledge', mode: 'local' },
    { feature: 'document_review', mode: 'mock' },
  ])
  renderRoute('/settings')
  await userEvent.selectOptions(screen.getByLabelText('运维知识库模式'), 'mock')
  await userEvent.click(screen.getByRole('button', { name: '保存运维知识库设置' }))
  expect(lastRequestJson()).toEqual({ mode: 'mock' })
  expect(screen.getByText('坐席辅助：当前 Mock')).toBeVisible()
  expect(screen.getByText('文档审核：当前 Mock')).toBeVisible()
})
```

- [ ] **Step 2: Run RED**

Run: `cd frontend && corepack pnpm vitest run src/test/SettingsPage.test.tsx`
Expected: FAIL because the page still exposes one global provider form.

- [ ] **Step 3: Implement three isolated cards**

Use independent form state keyed by feature. `local` requires a successful test response matching the exact candidate model fingerprint; `mock` saves immediately. Persisted active badges come only from query data, never unsaved form selection. Render Ollama base URL as `Docker 内网服务` and never render the literal internal URL.

- [ ] **Step 4: Run GREEN and contracts**

Run: `cd frontend && corepack pnpm vitest run src/test/SettingsPage.test.tsx && corepack pnpm build && corepack pnpm lint`
Run: `cd backend && py -3.11 -m pytest tests/test_settings_api.py -v`
Expected: PASS; payloads contain only feature and mode.

- [ ] **Step 5: Commit**

```bash
git add frontend/src/pages/SettingsPage.tsx frontend/src/api/types.ts frontend/src/styles/global.css frontend/src/test/SettingsPage.test.tsx backend/tests/test_settings_api.py
git commit -m "feat: configure model mode per workflow"
```

---

### Task 11: Collect and aggregate real 24-hour model monitoring

**Files:**
- Create: `backend/app/services/monitoring.py`
- Create: `backend/app/api/monitoring.py`
- Modify: `backend/app/main.py`
- Modify: `backend/app/schemas.py`
- Modify: `backend/pyproject.toml`
- Test: `backend/tests/test_monitoring.py`
- Test: `backend/tests/test_monitoring_api.py`

**Interfaces:**
- Consumes request metrics from Task 6, Ollama status from Task 4, and feature snapshots from Task 5.
- Produces `ResourceSampler.start()/stop()/sample_once()`.
- Produces `GET /api/monitoring/live` and `GET /api/monitoring/history?hours=24`.

- [ ] **Step 1: Write RED tests for real samples, aggregation, retention, and redaction**

```python
def test_history_keeps_only_24_hours_and_computes_p95(db_session, frozen_time):
    add_request_metrics(db_session, elapsed=[100, 200, 300, 400, 500], created_at=frozen_time.now)
    add_request_metrics(db_session, elapsed=[9999], created_at=frozen_time.now - timedelta(hours=25))
    history = MonitoringService(lambda: db_session).history(hours=24)
    assert history.total_requests == 5
    assert history.p95_elapsed_ms == 500
    assert all(point.created_at >= frozen_time.now - timedelta(hours=24) for point in history.requests)
```

Serialize all metric tables and assert sentinel prompts, documents, passwords, cookies, and API keys are absent.

- [ ] **Step 2: Run RED**

Run: `cd backend && py -3.11 -m pytest tests/test_monitoring.py tests/test_monitoring_api.py -v`
Expected: FAIL because monitoring service and endpoints do not exist.

- [ ] **Step 3: Implement the resource sampler without Docker Socket**

Add `psutil` to dependencies. Every 10 seconds record `psutil.cpu_percent()`, `virtual_memory().percent`, and `disk_usage('/').percent`, plus Ollama/model status and inference live counters. Use a daemon thread with `Event.wait(10)` so shutdown is prompt. Catch sampling errors into a fixed status and keep the next scheduled sample.

- [ ] **Step 4: Implement 24-hour aggregation and cleanup**

```python
def percentile_95(values: list[int]) -> int:
    if not values:
        return 0
    ordered = sorted(values)
    index = max(math.ceil(0.95 * len(ordered)) - 1, 0)
    return ordered[index]
```

Group request counts by 5-minute bucket, feature, mode, and result. Return average/P95 elapsed, average TTFT, queue wait, maximum queue depth, token totals when available, and resource series. Delete resource and request rows older than 24 hours after each successful sample; deletion failure must not stop the sampler.

- [ ] **Step 5: Run GREEN and lifecycle test**

Run: `cd backend && py -3.11 -m pytest tests/test_monitoring.py tests/test_monitoring_api.py tests/test_health.py -v`
Expected: PASS; app lifespan starts and stops the sampler without lingering threads.

- [ ] **Step 6: Commit**

```bash
git add backend/app/services/monitoring.py backend/app/api/monitoring.py backend/app/main.py backend/app/schemas.py backend/pyproject.toml backend/tests/test_monitoring.py backend/tests/test_monitoring_api.py
git commit -m "feat: add real local model monitoring metrics"
```

---

### Task 12: Build the accessible model-monitoring dashboard

**Files:**
- Create: `frontend/src/components/MetricLineChart.tsx`
- Create: `frontend/src/pages/ModelMonitoringPage.tsx`
- Modify: `frontend/src/api/types.ts`
- Modify: `frontend/src/app/router.tsx`
- Modify: `frontend/src/components/AppShell.tsx`
- Modify: `frontend/src/styles/global.css`
- Test: `frontend/src/test/MetricLineChart.test.tsx`
- Test: `frontend/src/test/ModelMonitoringPage.test.tsx`

**Interfaces:**
- Consumes `/api/monitoring/live` every 10 seconds and `/api/monitoring/history?hours=24` every 60 seconds.
- Produces `/monitoring` protected route and navigation item `本地模型监控`.

- [ ] **Step 1: Write RED chart and page tests**

```tsx
it('shows actual Ollama, queue, resource, and 24-hour request state', async () => {
  mockMonitoringLive({ ollama_available: true, model_installed: true, model_loaded: true, running: 1, queued: 2 })
  mockMonitoringHistory({ total_requests: 12, p95_elapsed_ms: 8300, success: 10, failed: 1, timed_out: 1 })
  renderRoute('/monitoring')
  expect(await screen.findByText('Qwen3 1.7B Q4')).toBeVisible()
  expect(screen.getByText('运行中 1 / 排队 2')).toBeVisible()
  expect(screen.getByRole('img', { name: '最近 24 小时请求趋势' })).toBeVisible()
  expect(screen.getByText('P95 8300 ms')).toBeVisible()
})
```

- [ ] **Step 2: Run RED**

Run: `cd frontend && corepack pnpm vitest run src/test/MetricLineChart.test.tsx src/test/ModelMonitoringPage.test.tsx`
Expected: FAIL because the route, chart, and page do not exist.

- [ ] **Step 3: Implement an accessible dependency-free SVG chart**

```tsx
export function MetricLineChart({ title, points }: Props) {
  const path = buildNormalizedPath(points, 640, 180)
  return (
    <figure className="metric-chart">
      <figcaption>{title}</figcaption>
      <svg role="img" aria-label={title} viewBox="0 0 640 180">
        <path d={path} fill="none" stroke="currentColor" strokeWidth="3" />
      </svg>
      <table className="sr-only"><tbody>{points.map(renderAccessibleRow)}</tbody></table>
    </figure>
  )
}
```

Render a meaningful empty state when no points exist; do not fabricate a line.

- [ ] **Step 4: Implement live cards and 24-hour sections**

Show Ollama/version, installed/loaded, model size, server CPU/memory/disk, running/queued/limit, feature modes, request totals, local/Mock share, average/TTFT/P95, result distribution, token totals, and collection error state. Label CPU/memory/disk `服务器整体资源`, not model-specific usage.

- [ ] **Step 5: Run GREEN, 1366×768 test, build, and lint**

Run: `cd frontend && corepack pnpm vitest run src/test/MetricLineChart.test.tsx src/test/ModelMonitoringPage.test.tsx && corepack pnpm test && corepack pnpm build && corepack pnpm lint`
Expected: PASS; no horizontal page overflow at 1366×768 and charts have accessible tables.

- [ ] **Step 6: Commit**

```bash
git add frontend/src/components/MetricLineChart.tsx frontend/src/pages/ModelMonitoringPage.tsx frontend/src/api/types.ts frontend/src/app/router.tsx frontend/src/components/AppShell.tsx frontend/src/styles/global.css frontend/src/test/MetricLineChart.test.tsx frontend/src/test/ModelMonitoringPage.test.tsx
git commit -m "feat: visualize local model health and history"
```

---

### Task 13: Integrate feature routing into all workflows and complete E2E coverage

**Files:**
- Modify: `backend/app/api/calls.py`
- Modify: `backend/app/api/knowledge.py`
- Modify: `backend/app/api/reviews.py`
- Modify: `backend/app/services/review_pipeline.py`
- Modify: `backend/app/services/review_executor.py`
- Modify: `backend/app/main.py`
- Modify: `frontend/e2e/demo-flow.spec.ts`
- Create: `frontend/e2e/local-model-knowledge.spec.ts`
- Modify: `frontend/e2e/visual-qa.spec.ts`
- Modify: `frontend/playwright.config.ts`
- Test: `backend/tests/test_calls_api.py`
- Test: `backend/tests/test_reviews_api.py`
- Test: `backend/tests/test_review_pipeline.py`

**Interfaces:**
- Consumes Task 5 router, Task 6 coordinator, Task 8 streaming knowledge, Task 2 auth, and Task 11 metrics.
- Produces one consistent routing/metrics contract across calls, knowledge, and review.
- E2E starts a deterministic fake Ollama HTTP service; no model download is required in CI.

- [ ] **Step 1: Write RED backend integration tests**

```python
def test_each_workflow_uses_its_own_feature_snapshot(client, feature_router_spy):
    run_call_suggestion(client)
    run_knowledge_query(client)
    run_document_review(client)
    assert feature_router_spy.features == [
        "call_assistant", "knowledge", "document_review"
    ]
```

Add cancellation tests proving call/knowledge streams release inference admission. Add a review retry test proving a task captures one feature snapshot per attempt and its audit/metrics mode matches the provider actually used.

- [ ] **Step 2: Run backend RED**

Run: `cd backend && py -3.11 -m pytest tests/test_calls_api.py tests/test_reviews_api.py tests/test_review_pipeline.py -v`
Expected: FAIL where global registry/coordinator wiring remains.

- [ ] **Step 3: Wire exact feature keys and preserve cancellation semantics**

Calls acquire `call_assistant`, knowledge acquires `knowledge`, and each review attempt acquires `document_review` once before parsing. Pass all local provider operations through `InferenceCoordinator`; Mock operations record metrics without consuming the local semaphore. Map queue full to 429 and timeout/provider unavailable to redacted 503 responses.

- [ ] **Step 4: Add deterministic fake Ollama E2E service**

Create a test-only FastAPI fixture launched by Playwright that implements `/api/version`, `/api/tags`, `/api/ps`, and `/v1/chat/completions`, including SSE and usage frames. Configure Playwright backend environment with auth enabled, known test hash, local knowledge mode, and fake Ollama URL. Do not add a production test bypass.

- [ ] **Step 5: Add full browser story**

The E2E must log in, show monitoring online, ask a recommended local-model question, observe retrieval then streamed answer and citation, upload a TXT document, wait for preview, approve it, ask a second question that cites the upload, change call mode without altering review mode, and return to monitoring to see the new request count. Add 1366×768 screenshots for login, knowledge management, and monitoring.

- [ ] **Step 6: Run complete verification and commit**

Run: `cd backend && py -3.11 -m pytest -q && py -3.11 -m ruff check app tests`
Run: `cd frontend && corepack pnpm test && corepack pnpm build && corepack pnpm lint && corepack pnpm exec playwright test`
Expected: all backend, frontend, and browser tests PASS with no leaked test services.

```bash
git add backend/app/api backend/app/services backend/app/main.py backend/tests frontend/e2e frontend/playwright.config.ts
git commit -m "feat: integrate local models across demo workflows"
```

---

### Task 14: Document, package, deploy, and verify the real server model

**Files:**
- Create: `scripts/generate-demo-auth.ps1`
- Modify: `.env.example`
- Modify: `README.md`
- Modify: `backend/tests/test_final_static.py`
- Modify: `backend/tests/test_windows_launcher.py`
- Modify: `docker-compose.yml`

**Interfaces:**
- Produces a one-time credential generator that prints username, plaintext password once, password hash, and session secret without writing them to disk.
- Produces operator commands for backup, model pull, deployment, health, logs, rollback, and credential rotation.
- Produces a verified server deployment under `/opt/local-llm-demo` without touching `everlink-api` on port 8000.

- [ ] **Step 1: Write RED packaging and secret-boundary tests**

```python
def test_compose_enables_auth_and_never_publishes_ollama():
    compose = yaml.safe_load((ROOT / "docker-compose.yml").read_text())
    backend_env = compose["services"]["backend"]["environment"]
    assert backend_env["DEMO_AUTH_REQUIRED"] == "${DEMO_AUTH_REQUIRED:-true}"
    assert "ports" not in compose["services"]["ollama"]
    assert "DEMO_AUTH_PASSWORD_HASH" not in json.dumps(compose["services"]["frontend"])
```

Add a Windows test that runs the credential generator, validates scrypt format and 32-byte session secret, and confirms no plaintext credential file was created.

- [ ] **Step 2: Run RED**

Run: `cd backend && py -3.11 -m pytest tests/test_final_static.py tests/test_windows_launcher.py -v`
Expected: FAIL because documentation/generator and final Compose auth wiring are incomplete.

- [ ] **Step 3: Implement the one-time credential generator**

The PowerShell script invokes Python 3.11 with an inline standard-library program using `secrets`, `hashlib.scrypt`, and `base64`. It prints a generated 20-character password once, the encoded scrypt hash, and a 32-byte URL-safe session secret. It must not call `Set-Content`, modify `.env`, or echo the server root password.

- [ ] **Step 4: Complete README operations and run static GREEN**

Document these exact server phases: back up `/opt/local-llm-demo` and its named data volume, set auth variables in server `.env`, start `ollama`, run `model-init`, confirm the exact tag with `docker compose exec ollama ollama list`, build application containers, log in, run a real cited question, inspect monitoring, restart Compose, confirm persistence, and rotate credentials. Document rollback to the saved Compose/source plus SQLite/upload backup. State that HTTP credentials are disposable.

Run: `cd backend && py -3.11 -m pytest tests/test_final_static.py tests/test_windows_launcher.py -v`
Run: `docker compose config --quiet`
Expected: tests PASS and Compose validates.

- [ ] **Step 5: Run pre-deployment completion gate**

Run: `cd backend && py -3.11 -m pytest -q && py -3.11 -m ruff check app tests`
Run: `cd frontend && corepack pnpm test && corepack pnpm build && corepack pnpm lint && corepack pnpm exec playwright test`
Run: `git diff --check && git status --short --branch`
Expected: all commands PASS and the worktree is clean after committing.

- [ ] **Step 6: Commit packaging, push the feature branch, and back up server state**

```bash
git add scripts/generate-demo-auth.ps1 .env.example README.md backend/tests/test_final_static.py backend/tests/test_windows_launcher.py docker-compose.yml
git commit -m "docs: package local model demo operations"
git push -u origin codex/local-model-ops-knowledge
```

Before server mutation, record current `docker compose ps`, copy the current source directory to a timestamped `/opt/backups/local-llm-demo-YYYYMMDD-HHMMSS`, and create an application-data archive. Do not delete or reset the existing named volume.

- [ ] **Step 7: Deploy and run real-model acceptance**

On the server, start `ollama`, run `model-init`, and require its zero exit status before rebuilding backend/frontend. Verify:

```bash
cd /opt/local-llm-demo
docker compose exec ollama ollama list
docker compose ps
curl -fsS http://127.0.0.1:8080/api/health
```

Then perform browser acceptance: unauthenticated redirect, login, model installed/loaded status, one recommended Qwen3 knowledge answer with citation, uploaded TXT preview/approval/new citation, independent mode switches, 24-hour monitoring update, Compose restart, and persistence. Confirm `ss -ltn` shows no host listener on 11434 and the existing port-8000 service remains running.

- [ ] **Step 8: Final review and handoff**

Generate a full diff review package from the pre-feature base through HEAD and request an independent code review. Fix every Critical and Important item, rerun Step 5, and report the public demo URL, disposable demo username/password, deployment commit, container states, real-model response timing, Docker volume names, backup path, and rollback commands. Never include the root password, password hash, session secret, cookies, or prompts in the report.

---

## Plan Completion Gate

The implementation is complete only when all fourteen task commits are present, the full backend/frontend/Playwright suites pass from a clean worktree, Compose validates, an independent full-branch review has no Critical or Important findings, and the real server runs the exact Qwen3 tag with Ollama private to the Compose network. A successful Mock-only test run is not sufficient for completion.
