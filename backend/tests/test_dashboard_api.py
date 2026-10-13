import json
from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app
from app.models import AuditEvent, ReviewTask


def test_dashboard_contains_required_metrics(tmp_path):
    with TestClient(create_app(Settings(runtime_dir=tmp_path))) as client:
        response = client.get("/api/dashboard")

    assert response.status_code == 200
    body = response.json()
    assert set(body) == {
        "service",
        "mode",
        "today_requests",
        "average_latency_ms",
        "citation_coverage",
        "review_counts",
        "queues",
    }
    assert set(body["queues"]) == {"realtime", "long_document"}


def test_dashboard_aggregates_today_events_knowledge_coverage_and_reviews(tmp_path):
    app = create_app(Settings(runtime_dir=tmp_path))
    now = datetime.now(timezone.utc)
    yesterday = now - timedelta(days=1)
    with TestClient(app) as client:
        with app.state.session_factory() as session:
            session.add_all(
                [
                    AuditEvent(
                        event_type="call.summary.completed",
                        result_status="completed",
                        request_summary="Call summary completed",
                        mode="demo",
                        elapsed_ms=100,
                        created_at=now,
                    ),
                    AuditEvent(
                        event_type="knowledge.query.completed",
                        result_status="answered",
                        request_summary="Knowledge query processed",
                        mode="demo",
                        elapsed_ms=300,
                        created_at=now,
                    ),
                    AuditEvent(
                        event_type="knowledge.query.no_evidence",
                        result_status="no_evidence",
                        request_summary="Knowledge query processed",
                        mode="demo",
                        created_at=now,
                    ),
                    AuditEvent(
                        event_type="review.submitted",
                        result_status="queued",
                        request_summary="Review submitted",
                        mode="demo",
                        elapsed_ms=50,
                        created_at=now,
                    ),
                    AuditEvent(
                        event_type="review.stage.parsing",
                        result_status="parsing",
                        request_summary="Review stage updated",
                        mode="demo",
                        elapsed_ms=60,
                        created_at=now,
                    ),
                    AuditEvent(
                        event_type="settings.provider.tested",
                        result_status="available",
                        request_summary="Provider connection tested",
                        mode="demo",
                        elapsed_ms=70,
                        created_at=now,
                    ),
                    AuditEvent(
                        event_type="call.summary.completed",
                        result_status="completed",
                        request_summary="Call summary completed",
                        mode="demo",
                        elapsed_ms=999,
                        created_at=yesterday,
                    ),
                    AuditEvent(
                        event_type="knowledge.query.completed",
                        result_status="answered",
                        request_summary="Knowledge query processed",
                        mode="demo",
                        elapsed_ms=999,
                        created_at=yesterday,
                    ),
                    ReviewTask(
                        document_filename="waiting.txt",
                        document_type="technical_proposal",
                        file_path=str(tmp_path / "waiting.txt"),
                        status="queued",
                    ),
                    ReviewTask(
                        document_filename="running.txt",
                        document_type="technical_proposal",
                        file_path=str(tmp_path / "running.txt"),
                        status="reviewing",
                        stage="reviewing",
                        progress=50,
                    ),
                    ReviewTask(
                        document_filename="finished.txt",
                        document_type="technical_proposal",
                        file_path=str(tmp_path / "finished.txt"),
                        status="completed",
                        stage="completed",
                        progress=100,
                    ),
                ]
            )
            session.commit()

        body = client.get("/api/dashboard").json()

    assert body["today_requests"] == 5
    assert body["average_latency_ms"] == 130
    assert body["citation_coverage"] == 50
    assert body["review_counts"] == {"queued": 1, "reviewing": 1, "completed": 1}
    assert body["queues"] == {"realtime": 0, "long_document": 2}


def test_audit_events_do_not_expose_inputs_or_secrets(tmp_path):
    app = create_app(Settings(runtime_dir=tmp_path))
    with TestClient(app) as client:
        with app.state.session_factory() as session:
            session.add(
                AuditEvent(
                    event_type="knowledge.query.completed",
                    result_status="answered",
                    request_summary="question=full user question; api_key=super-secret",
                    mode="demo",
                    reference_id="source-1",
                )
            )
            session.commit()

        response = client.get("/api/audit-events?limit=50")

    assert response.status_code == 200
    events = response.json()
    serialized = json.dumps(events, ensure_ascii=False)
    assert "api_key" not in serialized
    assert "super-secret" not in serialized
    assert "full user question" not in serialized
    assert set(events[0]) == {
        "id",
        "event_type",
        "request_summary",
        "reference_id",
        "mode",
        "elapsed_ms",
        "result_status",
        "created_at",
    }


def test_audit_events_render_legacy_and_feature_provider_modes(tmp_path):
    app = create_app(Settings(runtime_dir=tmp_path))
    with TestClient(app) as client:
        with app.state.session_factory() as session:
            session.add_all(
                [
                    AuditEvent(
                        event_type="settings.provider.updated",
                        result_status="completed",
                        request_summary="Provider settings updated",
                        mode="demo",
                    ),
                    AuditEvent(
                        event_type="settings.feature.updated",
                        result_status="completed",
                        request_summary="Feature provider settings updated",
                        mode="local",
                    ),
                ]
            )
            session.commit()

        response = client.get("/api/audit-events")

    assert {event["mode"] for event in response.json()} == {"demo", "local"}


def test_audit_events_are_newest_first_and_limit_is_bounded(tmp_path):
    app = create_app(Settings(runtime_dir=tmp_path))
    now = datetime.now(timezone.utc)
    with TestClient(app) as client:
        with app.state.session_factory() as session:
            session.add_all(
                [
                    AuditEvent(
                        event_type="call.summary.completed",
                        result_status="completed",
                        request_summary="Call summary completed",
                        mode="demo",
                        created_at=now - timedelta(seconds=1),
                    ),
                    AuditEvent(
                        event_type="knowledge.query.completed",
                        result_status="answered",
                        request_summary="Knowledge query processed",
                        mode="demo",
                        created_at=now,
                    ),
                ]
            )
            session.commit()

        response = client.get("/api/audit-events?limit=1")
        invalid_limit = client.get("/api/audit-events?limit=0")

    assert response.status_code == 200
    assert [event["event_type"] for event in response.json()] == [
        "knowledge.query.completed"
    ]
    assert invalid_limit.status_code == 422
