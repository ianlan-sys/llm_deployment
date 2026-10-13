import asyncio
import json
from datetime import datetime, timezone
from threading import Event, Thread
from time import perf_counter, sleep
from types import SimpleNamespace
from uuid import UUID

import httpx
from fastapi.testclient import TestClient
import pytest
from sqlalchemy import select, text

from app.api import calls
from app.config import Settings
from app.main import create_app
from app.models import AuditEvent, ModelRequestMetric
from app.providers.base import ProviderUnavailable, ProviderUsage
from app.providers.feature_router import FeatureProviderSnapshot
from app.providers.mock import MockProvider
from app.providers.openai_compatible import OpenAICompatibleProvider
from app.schemas import CallSuggestionRequest
from app.seed import seed_demo_data
from app.services.audit import AuditService
from app.services.inference import InferenceCancellation, InferenceCoordinator


def _events(payload: str) -> list[tuple[str, dict]]:
    events: list[tuple[str, dict]] = []
    for message in payload.strip().split("\n\n"):
        lines = dict(line.split(": ", 1) for line in message.splitlines())
        events.append((lines["event"], json.loads(lines["data"])))
    return events


def test_call_scenarios_expose_seeded_uuid_ids(tmp_path):
    with TestClient(create_app(Settings(runtime_dir=tmp_path))) as client:
        response = client.get("/api/demo/call-scenarios")

    assert response.status_code == 200
    scenarios = response.json()
    assert len(scenarios) == 4
    assert {scenario["id"] for scenario in scenarios}
    for scenario in scenarios:
        assert str(UUID(scenario["id"])) == scenario["id"]
        assert {"id", "name", "description", "category", "priority", "status"} <= set(scenario)


def test_call_scenarios_do_not_require_a_configured_model_provider(tmp_path):
    with TestClient(create_app(Settings(runtime_dir=tmp_path, provider_mode="real"))) as client:
        response = client.get("/api/demo/call-scenarios")

    assert response.status_code == 200
    assert len(response.json()) == 4


def test_call_suggestion_stream_has_meta_delta_and_done(tmp_path):
    with TestClient(create_app(Settings(runtime_dir=tmp_path))) as client:
        scenario_id = client.get("/api/demo/call-scenarios").json()[0]["id"]
        with client.stream("POST", "/api/calls/suggestions/stream", json={
            "scenario_id": scenario_id,
            "transcript": "The customer reports that the core business service is slow.",
        }) as response:
            payload = "".join(response.iter_text())

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    events = _events(payload)
    assert [event for event, _ in events][0] == "meta"
    assert "delta" in [event for event, _ in events]
    assert events[-1][0] == "done"
    meta = events[0][1]
    assert set(meta) == {
        "request_id", "session_id", "mode", "template_ids", "templates", "started_at"
    }
    assert meta["mode"] == "mock"
    assert meta["template_ids"]
    assert [template["id"] for template in meta["templates"]] == meta["template_ids"]
    assert all(template["title"] and template["content"] for template in meta["templates"])
    assert all(str(UUID(template_id)) == template_id for template_id in meta["template_ids"])
    assert events[-1][1]["elapsed_ms"] >= 0


def test_each_workflow_uses_its_own_feature_snapshot(tmp_path, monkeypatch):
    app = create_app(Settings(runtime_dir=tmp_path))
    with TestClient(app) as client:
        router = app.state.feature_provider_router
        with app.state.session_factory() as session:
            router.configure_and_persist(session, "knowledge", "mock")
        original_snapshot = router.snapshot
        features = []

        def recording_snapshot(feature):
            features.append(feature)
            return original_snapshot(feature)

        monkeypatch.setattr(router, "snapshot", recording_snapshot)

        scenario_id = client.get("/api/demo/call-scenarios").json()[0]["id"]
        call = client.post(
            "/api/calls/suggestions/stream",
            json={
                "scenario_id": scenario_id,
                "transcript": "客户反馈设备故障。",
            },
        )
        knowledge = client.post(
            "/api/knowledge/query",
            json={"question": "核心交换机 VLAN 如何划分？"},
        )
        review = client.post(
            "/api/reviews",
            data={"document_type": "technical_proposal"},
            files={
                "file": (
                    "proposal.txt",
                    "一、项目范围\n范围待补充。".encode(),
                    "text/plain",
                )
            },
        )
        task = _wait_for_review(client, review.json())

    assert call.status_code == 200
    assert knowledge.status_code == 200
    assert task["status"] == "completed"
    assert features == [
        "call_assistant",
        "knowledge",
        "document_review",
    ]


def test_call_operations_record_mock_metrics_without_local_admission(
    tmp_path, monkeypatch
):
    app = create_app(Settings(runtime_dir=tmp_path))
    with TestClient(app) as client:
        def reject_local_admission(*_args, **_kwargs):
            raise AssertionError("mock work must not acquire local admission")

        monkeypatch.setattr(
            app.state.inference_coordinator,
            "_acquire",
            reject_local_admission,
        )
        scenario_id = client.get("/api/demo/call-scenarios").json()[0]["id"]
        stream = client.post(
            "/api/calls/suggestions/stream",
            json={
                "scenario_id": scenario_id,
                "transcript": "客户反馈设备故障。",
            },
        )
        meta = _events(stream.text)[0][1]
        summary = client.post(
            "/api/calls/summary",
            json={
                "session_id": meta["session_id"],
                "scenario_id": scenario_id,
                "original_transcript": "客户反馈设备故障。",
                "accepted_reply": "已登记并安排工程师跟进。",
            },
        )
        metrics = _wait_for_metrics(
            app,
            feature="call_assistant",
            expected=2,
        )

    assert stream.status_code == 200
    assert summary.status_code == 200
    assert [
        (metric.operation, metric.mode, metric.result_status)
        for metric in metrics
    ] == [
        ("call_suggestion", "mock", "success"),
        ("call_summary", "mock", "success"),
    ]


class _UnavailableProvider:
    @property
    def last_usage(self):
        return ProviderUsage(input_tokens=None, output_tokens=None)

    def reset_usage(self):
        return None

    def stream_call_suggestion(self, call_context, templates):
        raise ProviderUnavailable("private provider detail")
        yield

    def generate_call_summary(self, call_context):
        raise ProviderUnavailable("private provider detail")


def _install_call_snapshot(monkeypatch, app, provider, mode="local"):
    router = app.state.feature_provider_router
    original_snapshot = router.snapshot
    snapshot = FeatureProviderSnapshot(
        feature="call_assistant",
        mode=mode,
        model="test-model" if mode == "local" else None,
        provider=provider,
    )
    monkeypatch.setattr(
        router,
        "snapshot",
        lambda feature: (
            snapshot
            if feature == "call_assistant"
            else original_snapshot(feature)
        ),
    )
    return snapshot


def test_call_suggestion_stream_reports_an_unavailable_provider(
    tmp_path, monkeypatch
):
    app = create_app(Settings(runtime_dir=tmp_path))
    with TestClient(app, raise_server_exceptions=False) as client:
        _install_call_snapshot(
            monkeypatch,
            app,
            _UnavailableProvider(),
        )
        scenario_id = client.get("/api/demo/call-scenarios").json()[0]["id"]
        with client.stream("POST", "/api/calls/suggestions/stream", json={
            "scenario_id": scenario_id,
            "transcript": "A customer needs help.",
        }) as response:
            payload = "".join(response.iter_text())
        with app.state.session_factory() as session:
            audit_events = list(session.scalars(select(AuditEvent)))

    assert response.status_code == 200
    assert [event for event, _ in _events(payload)] == ["meta", "error"]
    assert _events(payload)[-1][1] == {"message": "模型服务暂不可用，请检查配置后重试。"}
    assert [(event.event_type, event.result_status) for event in audit_events] == [
        ("call.suggestion.failed", "failed"),
    ]


def test_unknown_call_scenario_uses_simplified_chinese_detail(tmp_path):
    with TestClient(create_app(Settings(runtime_dir=tmp_path))) as client:
        response = client.post("/api/calls/suggestions/stream", json={
            "scenario_id": str(UUID(int=0)),
            "transcript": "A customer needs help.",
        })

    assert response.status_code == 404
    assert response.json() == {"detail": "未找到通话场景。"}


def test_summary_reports_provider_failure_without_recording_transcript(
    tmp_path, monkeypatch
):
    transcript = "do not audit this customer transcript"
    app = create_app(Settings(runtime_dir=tmp_path))
    with TestClient(app, raise_server_exceptions=False) as client:
        _install_call_snapshot(
            monkeypatch,
            app,
            _UnavailableProvider(),
        )
        scenario_id = client.get("/api/demo/call-scenarios").json()[0]["id"]
        stream = client.post("/api/calls/suggestions/stream", json={
            "scenario_id": scenario_id,
            "transcript": transcript,
        })
        session_id = _events(stream.text)[0][1]["session_id"]
        response = client.post("/api/calls/summary", json={
            "session_id": session_id,
            "scenario_id": scenario_id,
            "original_transcript": transcript,
            "accepted_reply": "edited reply",
        })
        with app.state.session_factory() as session:
            audit_events = list(session.scalars(select(AuditEvent)))

    assert response.status_code == 503
    assert response.json() == {"detail": "模型服务暂不可用，请稍后重试。"}
    assert [(event.event_type, event.result_status) for event in audit_events] == [
        ("call.suggestion.failed", "failed"),
        ("call.summary.failed", "failed"),
    ]
    assert all(transcript not in event.request_summary for event in audit_events)


class _BlockingRequest:
    async def is_disconnected(self) -> bool:
        await asyncio.Event().wait()
        return False


class _ConnectedRequest:
    async def is_disconnected(self) -> bool:
        return False


class _RouteRequest(_ConnectedRequest):
    def __init__(self, app):
        self.app = app


class _BlockingCallBody(httpx.AsyncByteStream):
    def __init__(self):
        self.started = Event()
        self.closed = Event()

    async def __aiter__(self):
        self.started.set()
        await asyncio.Future()
        yield b"data: [DONE]\n\n"

    async def aclose(self):
        self.closed.set()


class _BlockingCallBodyTransport(httpx.AsyncBaseTransport):
    def __init__(self, body):
        self.body = body

    async def handle_async_request(self, request):
        return httpx.Response(
            200,
            request=request,
            headers={"content-type": "text/event-stream"},
            stream=self.body,
        )


class _BlockingCallSetupTransport(httpx.AsyncBaseTransport):
    def __init__(self):
        self.started = Event()
        self.cancel_observed = Event()
        self.finished = Event()

    async def handle_async_request(self, request):
        self.started.set()
        try:
            await asyncio.Future()
        except asyncio.CancelledError:
            self.cancel_observed.set()
            raise
        finally:
            self.finished.set()


@pytest.mark.asyncio
async def test_call_endpoint_cancellation_aborts_local_http_stream_and_releases_admission(
    db_session,
):
    seed_demo_data(db_session)
    scenario_id = calls.CallAssistant(db_session).scenarios()[0].id
    body = _BlockingCallBody()

    class ScenarioProvider(OpenAICompatibleProvider):
        def _new_call_transport(self):
            return _BlockingCallBodyTransport(body)

    provider = ScenarioProvider(
        base_url="http://model.test/v1",
        model="test-local-model",
        timeout_seconds=2,
    )
    snapshot = FeatureProviderSnapshot(
        feature="call_assistant",
        mode="local",
        model="test-local-model",
        provider=provider,
    )
    coordinator = InferenceCoordinator(
        lambda: db_session,
        timeout_seconds=2,
    )

    class FeatureRouter:
        def snapshot(self, feature):
            assert feature == "call_assistant"
            return snapshot

    app = SimpleNamespace(
        state=SimpleNamespace(
            feature_provider_router=FeatureRouter(),
            inference_coordinator=coordinator,
        )
    )
    response = await calls.stream_suggestions(
        CallSuggestionRequest(
            scenario_id=scenario_id,
            transcript="核心业务服务响应缓慢",
        ),
        _RouteRequest(app),
        db_session,
    )
    events = response.body_iterator

    try:
        assert "event: meta" in await anext(events)
        pending_next = asyncio.create_task(anext(events))
        assert await asyncio.to_thread(body.started.wait, 1)
        pending_next.cancel()
        with pytest.raises(asyncio.CancelledError):
            await pending_next
        deadline = perf_counter() + 0.5
        while (
            coordinator.live_state().running
            or coordinator.live_state().queued
        ) and perf_counter() < deadline:
            await asyncio.sleep(0.01)
        assert body.closed.is_set()
        assert coordinator.live_state().running == 0
        assert coordinator.live_state().queued == 0
    finally:
        await events.aclose()
        assert coordinator.shutdown(timeout_seconds=1)


@pytest.mark.asyncio
async def test_call_endpoint_cancellation_aborts_before_local_response_exists(
    db_session,
):
    seed_demo_data(db_session)
    scenario_id = calls.CallAssistant(db_session).scenarios()[0].id
    transport = _BlockingCallSetupTransport()

    class ScenarioProvider(OpenAICompatibleProvider):
        def _new_call_transport(self):
            return transport

    provider = ScenarioProvider(
        base_url="http://model.test/v1",
        model="test-local-model",
        timeout_seconds=2,
    )
    snapshot = FeatureProviderSnapshot(
        feature="call_assistant",
        mode="local",
        model="test-local-model",
        provider=provider,
    )
    coordinator = InferenceCoordinator(
        lambda: db_session,
        timeout_seconds=2,
    )

    class FeatureRouter:
        def snapshot(self, feature):
            assert feature == "call_assistant"
            return snapshot

    app = SimpleNamespace(
        state=SimpleNamespace(
            feature_provider_router=FeatureRouter(),
            inference_coordinator=coordinator,
        )
    )
    response = await calls.stream_suggestions(
        CallSuggestionRequest(
            scenario_id=scenario_id,
            transcript="核心业务服务响应缓慢",
        ),
        _RouteRequest(app),
        db_session,
    )
    events = response.body_iterator

    try:
        assert "event: meta" in await anext(events)
        pending_next = asyncio.create_task(anext(events))
        assert await asyncio.to_thread(transport.started.wait, 1)
        pending_next.cancel()
        with pytest.raises(asyncio.CancelledError):
            await pending_next
        assert transport.cancel_observed.wait(timeout=0.3)
        assert transport.finished.is_set()
        assert coordinator.live_state().running == 0
        assert coordinator.live_state().queued == 0
    finally:
        await events.aclose()
        assert coordinator.shutdown(timeout_seconds=1)


@pytest.mark.asyncio
async def test_cancelled_stream_records_one_cancelled_audit(db_session):
    events = calls.call_suggestion_events(
        provider_stream=iter(["first delta"]),
        request=_BlockingRequest(),
        audit=AuditService(db_session),
        request_id="request-1",
        mode="demo",
        scenario_id=str(UUID(int=1)),
        template_ids=[],
        started_at=datetime.now(timezone.utc).isoformat(),
        started=perf_counter(),
    )

    assert "event: meta" in await anext(events)
    pending_next = asyncio.create_task(anext(events))
    await asyncio.sleep(0)
    pending_next.cancel()
    with pytest.raises(asyncio.CancelledError):
        await pending_next

    audit_events = list(db_session.scalars(select(AuditEvent)))
    assert [(event.event_type, event.result_status) for event in audit_events] == [
        ("call.suggestion.cancelled", "cancelled"),
    ]


class _SlowCloseableIterator:
    def __init__(self):
        self.closed = False

    def __iter__(self):
        return self

    def __next__(self):
        sleep(0.20)
        return "slow delta"

    def close(self):
        self.closed = True


class _CancellableBlockingIterator:
    def __init__(self):
        self.waiting = Event()
        self.released = Event()
        self._first = True

    def __iter__(self):
        return self

    def __next__(self):
        if self._first:
            self._first = False
            return "first delta"
        self.waiting.set()
        self.released.wait(timeout=2)
        raise StopIteration

    def cancel(self):
        self.released.set()

    def close(self):
        self.cancel()


@pytest.mark.asyncio
async def test_cancelled_call_stream_releases_inference_admission(db_session):
    coordinator = InferenceCoordinator(lambda: db_session, timeout_seconds=2)
    cancellation = InferenceCancellation()
    upstream = _CancellableBlockingIterator()
    snapshot = FeatureProviderSnapshot(
        feature="call_assistant",
        mode="local",
        model="test-local-model",
        provider=MockProvider(delay_ms=0),
    )
    coordinated = coordinator.stream(
        "call_assistant",
        "call_suggestion",
        snapshot,
        lambda: upstream,
        cancellation=cancellation,
    )
    events = calls.call_suggestion_events(
        provider_stream=coordinated,
        request=_ConnectedRequest(),
        audit=AuditService(db_session),
        request_id="request-1",
        mode="local",
        scenario_id=str(UUID(int=1)),
        template_ids=[],
        started_at=datetime.now(timezone.utc).isoformat(),
        started=perf_counter(),
        cancel_stream=lambda: (
            cancellation.cancel(),
            upstream.cancel(),
        ),
    )

    try:
        assert "event: meta" in await anext(events)
        assert "first delta" in await anext(events)
        assert upstream.waiting.wait(timeout=1)
        pending_next = asyncio.create_task(anext(events))
        await asyncio.sleep(0)
        pending_next.cancel()
        with pytest.raises(asyncio.CancelledError):
            await pending_next
        deadline = perf_counter() + 1
        while (
            coordinator.live_state().running
            or coordinator.live_state().queued
        ) and perf_counter() < deadline:
            await asyncio.sleep(0.01)
        assert coordinator.live_state().running == 0
        assert coordinator.live_state().queued == 0
    finally:
        upstream.cancel()
        await events.aclose()
        assert coordinator.shutdown(timeout_seconds=1)


@pytest.mark.asyncio
async def test_provider_iteration_does_not_block_event_loop_and_cancel_closes_iterator(db_session):
    provider_stream = _SlowCloseableIterator()
    events = calls.call_suggestion_events(
        provider_stream=provider_stream,
        request=_BlockingRequest(),
        audit=AuditService(db_session),
        request_id="request-1",
        mode="demo",
        scenario_id=str(UUID(int=1)),
        template_ids=[],
        started_at=datetime.now(timezone.utc).isoformat(),
        started=perf_counter(),
    )

    assert "event: meta" in await anext(events)
    pending_next = asyncio.create_task(anext(events))
    started = perf_counter()
    await asyncio.sleep(0.02)
    assert perf_counter() - started < 0.10
    cancel_started = perf_counter()
    pending_next.cancel()
    with pytest.raises(asyncio.CancelledError):
        await pending_next
    assert perf_counter() - cancel_started < 0.10
    assert provider_stream.closed is True


def test_health_remains_responsive_during_a_delayed_provider_stream(tmp_path, monkeypatch):
    started_streaming = Event()

    class DelayedProvider:
        @property
        def last_usage(self):
            return ProviderUsage(input_tokens=None, output_tokens=None)

        def reset_usage(self):
            return None

        def stream_call_suggestion(self, call_context, templates):
            started_streaming.set()
            sleep(0.35)
            yield "done"

    app = create_app(Settings(runtime_dir=tmp_path))
    with TestClient(app) as client:
        _install_call_snapshot(
            monkeypatch,
            app,
            DelayedProvider(),
        )
        scenario_id = client.get("/api/demo/call-scenarios").json()[0]["id"]

        worker = Thread(
            target=lambda: client.post(
                "/api/calls/suggestions/stream",
                json={"scenario_id": scenario_id, "transcript": "故障报修"},
            ),
            daemon=True,
        )
        worker.start()
        assert started_streaming.wait(timeout=1)
        started = perf_counter()
        health = client.get("/api/health")
        elapsed = perf_counter() - started
        worker.join(timeout=1)

    assert health.status_code == 200
    assert elapsed < 0.15


def test_summary_has_required_fields_and_is_audited(tmp_path):
    app = create_app(Settings(runtime_dir=tmp_path))
    with TestClient(app) as client:
        scenario_id = client.get("/api/demo/call-scenarios").json()[0]["id"]
        stream = client.post("/api/calls/suggestions/stream", json={
            "scenario_id": scenario_id,
            "transcript": "The customer reported a fault.",
        })
        meta = _events(stream.text)[0][1]
        suggestion = "".join(
            data["content"] for event, data in _events(stream.text) if event == "delta"
        )
        adoption = client.put(
            f"/api/calls/sessions/{meta['session_id']}/adoption",
            json={"accepted_reply": suggestion},
        )
        edited_reply = f"{suggestion}已人工补充回访时限。"
        response = client.post("/api/calls/summary", json={
            "session_id": meta["session_id"],
            "scenario_id": scenario_id,
            "original_transcript": "The customer reported a fault.",
            "accepted_reply": edited_reply,
        })
        session_response = client.get(f"/api/calls/sessions/{meta['session_id']}")
        with app.state.session_factory() as session:
            audit_events = list(session.scalars(select(AuditEvent)))
            persisted_id = session.scalar(
                text("SELECT id FROM call_sessions WHERE id = :id"),
                {"id": meta["session_id"]},
            )

    assert response.status_code == 200
    assert set(response.json()) == {"appeal", "process", "result", "follow_up"}
    assert adoption.status_code == 200
    assert session_response.status_code == 200
    body = session_response.json()
    assert body["scenario_id"] == scenario_id
    assert body["transcript"] == "The customer reported a fault."
    assert body["suggestion"] == suggestion
    assert body["template_references"] == meta["templates"]
    assert body["provider_mode"] == "mock"
    assert body["request_id"] == meta["request_id"]
    assert body["ttft_ms"] >= 0 and body["elapsed_ms"] >= body["ttft_ms"]
    assert body["adoption_status"] == "edited"
    assert body["accepted_reply"] == edited_reply
    assert body["summary"] == response.json()
    assert persisted_id == meta["session_id"]
    assert {event.event_type for event in audit_events} >= {
        "call.suggestion.completed", "call.adoption.updated", "call.summary.completed"
    }
    serialized_audit = json.dumps(
        [event.request_summary for event in audit_events], ensure_ascii=False
    )
    assert "The customer reported a fault." not in serialized_audit
    assert edited_reply not in serialized_audit


def _wait_for_review(client, task):
    deadline = perf_counter() + 2
    while task["status"] not in {"completed", "failed"} and perf_counter() < deadline:
        sleep(0.01)
        task = client.get(f"/api/reviews/{task['id']}").json()
    return task


def _wait_for_metrics(app, *, feature, expected):
    deadline = perf_counter() + 2
    rows = []
    while len(rows) < expected and perf_counter() < deadline:
        with app.state.session_factory() as session:
            rows = list(
                session.scalars(
                    select(ModelRequestMetric)
                    .where(ModelRequestMetric.feature == feature)
                    .order_by(ModelRequestMetric.created_at)
                )
            )
        if len(rows) < expected:
            sleep(0.01)
    return rows
