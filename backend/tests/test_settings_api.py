import json
from threading import Event, Thread

from fastapi.testclient import TestClient
from pydantic import SecretStr
import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker

from app.config import Settings
from app.main import create_app
from app.models import AppSetting, AuditEvent, Base
from app.providers.mock import MockProvider
from app.providers.base import ProviderUnavailable
from app.providers.openai_compatible import OpenAICompatibleProvider
from app.providers.registry import ProviderRegistry


def test_settings_response_never_returns_api_key(tmp_path):
    with TestClient(create_app(Settings(runtime_dir=tmp_path))) as client:
        response = client.put("/api/settings/provider", json={
            "mode": "real",
            "base_url": "http://127.0.0.1:8001/v1",
            "model": "local-model",
        })

    assert response.status_code == 200
    assert "api_key" not in response.json()


def test_provider_settings_reject_browser_api_key_ingress_without_echoing_it(tmp_path):
    browser_secret = "browser-only-secret"
    with TestClient(create_app(Settings(runtime_dir=tmp_path))) as client:
        response = client.put("/api/settings/provider", json={
            "mode": "real",
            "base_url": "http://127.0.0.1:8001/v1",
            "model": "local-model",
            "api_key": browser_secret,
        })

    assert response.status_code == 422
    assert browser_secret not in response.text


def test_provider_settings_persist_only_non_sensitive_values(tmp_path):
    app = create_app(Settings(runtime_dir=tmp_path))
    with TestClient(app) as client:
        response = client.put("/api/settings/provider", json={
            "mode": "real",
            "base_url": "http://127.0.0.1:8001/v1",
            "model": "local-model",
        })
        assert response.json() == {
            "mode": "real",
            "base_url": "http://127.0.0.1:8001/v1",
            "model": "local-model",
        }

        settings = {row.key: row.value for row in client.app.state.session_factory().scalars(select(AppSetting))}

    assert settings == {
        "provider_mode": "real",
        "provider_base_url": "http://127.0.0.1:8001/v1",
        "provider_model": "local-model",
    }
    assert app.state.provider_registry.api_key is None


def test_get_settings_returns_persisted_values_without_api_key(tmp_path):
    with TestClient(create_app(Settings(runtime_dir=tmp_path))) as client:
        client.put("/api/settings/provider", json={
            "mode": "demo",
            "base_url": "http://127.0.0.1:8001/v1",
            "model": "local-model",
        })
        response = client.get("/api/settings/provider")

    assert response.status_code == 200
    assert response.json() == {
        "mode": "demo",
        "base_url": "http://127.0.0.1:8001/v1",
        "model": "local-model",
    }


def test_demo_connection_test_is_offline_and_available(tmp_path):
    with TestClient(create_app(Settings(runtime_dir=tmp_path))) as client:
        response = client.post("/api/settings/provider/test", json={"mode": "demo"})

    assert response.status_code == 200
    assert response.json()["available"] is True


def test_environment_api_key_is_moved_to_registry_memory(tmp_path, monkeypatch):
    monkeypatch.setenv("DEMO_OPENAI_API_KEY", "server-only-secret")
    settings = Settings(runtime_dir=tmp_path)
    assert isinstance(settings.openai_api_key, SecretStr)
    app = create_app(settings)

    assert app.state.provider_registry.api_key == "server-only-secret"
    assert app.state.settings.openai_api_key is None


def test_real_provider_validation_error_is_simplified_chinese(tmp_path):
    with TestClient(create_app(Settings(runtime_dir=tmp_path))) as client:
        response = client.put("/api/settings/provider", json={"mode": "real"})

    assert response.status_code == 422
    assert response.json() == {"detail": "真实模型模式需要填写服务地址和模型名称"}


def test_settings_update_persists_before_swapping_registry_snapshot(tmp_path, monkeypatch):
    engine = create_engine(f"sqlite:///{tmp_path / 'registry.db'}")
    Base.metadata.create_all(engine)
    registry = ProviderRegistry(mode="demo")
    with Session(engine) as session:
        monkeypatch.setattr(session, "commit", lambda: (_ for _ in ()).throw(RuntimeError("commit failed")))
        with pytest.raises(RuntimeError, match="commit failed"):
            registry.configure_and_persist(
                session,
                mode="real",
                base_url="http://127.0.0.1:8001/v1",
                model="local-model",
            )

    snapshot = registry.snapshot()
    assert snapshot.mode == "demo"
    assert isinstance(snapshot.provider, MockProvider)
    engine.dispose()


def test_registry_snapshot_keeps_mode_and_provider_metadata_consistent_under_concurrency(tmp_path):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'registry-concurrent.db'}",
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine)
    registry = ProviderRegistry(mode="demo")
    finished = Event()
    mismatches = []

    def writer():
        for index in range(30):
            mode = "real" if index % 2 else "demo"
            with factory() as session:
                registry.configure_and_persist(
                    session,
                    mode=mode,
                    base_url="http://127.0.0.1:8001/v1",
                    model=f"model-{index}",
                )
        finished.set()

    worker = Thread(target=writer, daemon=True)
    worker.start()
    while not finished.is_set():
        snapshot = registry.snapshot()
        if snapshot.mode == "demo" and not isinstance(snapshot.provider, MockProvider):
            mismatches.append(snapshot)
        if snapshot.mode == "real" and not isinstance(snapshot.provider, OpenAICompatibleProvider):
            mismatches.append(snapshot)
    worker.join(timeout=2)

    assert mismatches == []
    engine.dispose()


def test_settings_change_and_connection_test_have_fixed_redacted_audits(tmp_path):
    app = create_app(Settings(runtime_dir=tmp_path))
    with TestClient(app) as client:
        client.post("/api/settings/provider/test", json={"mode": "demo"})
        client.put("/api/settings/provider", json={
            "mode": "demo",
            "base_url": "http://127.0.0.1:8001/v1",
            "model": "local-model",
        })
        with app.state.session_factory() as session:
            events = list(
                session.scalars(
                    select(AuditEvent).where(AuditEvent.event_type.startswith("settings."))
                )
            )

    assert [(event.event_type, event.result_status) for event in events] == [
        ("settings.provider.tested", "available"),
        ("settings.provider.updated", "completed"),
    ]
    serialized = json.dumps([event.request_summary for event in events])
    assert "127.0.0.1" not in serialized
    assert "local-model" not in serialized


def test_feature_settings_return_defaults_in_stable_order(tmp_path):
    with TestClient(create_app(Settings(runtime_dir=tmp_path))) as client:
        response = client.get("/api/settings/features")

    assert response.status_code == 200
    assert [(item["feature"], item["mode"]) for item in response.json()] == [
        ("call_assistant", "mock"),
        ("knowledge", "local"),
        ("document_review", "mock"),
    ]


def test_feature_local_update_requires_the_exact_installed_model(tmp_path, monkeypatch):
    app = create_app(Settings(runtime_dir=tmp_path))
    with TestClient(app) as client:
        monkeypatch.setattr(app.state.ollama_client, "tags", lambda: [])
        response = client.put(
            "/api/settings/features/call_assistant",
            json={"mode": "local"},
        )

    assert response.status_code == 503
    assert response.json()["detail"]
    assert app.state.feature_provider_router.snapshot("call_assistant").mode == "mock"


@pytest.mark.parametrize(
    ("method", "path", "expected_status"),
    [
        ("put", "/api/settings/features/call_assistant", 503),
        ("post", "/api/settings/features/call_assistant/test", 200),
    ],
)
def test_feature_local_exact_model_absence_has_a_fixed_non_reflective_detail(
    tmp_path, monkeypatch, method, path, expected_status
):
    app = create_app(Settings(runtime_dir=tmp_path))
    candidate_model = "missing-model-that-must-not-leak"
    with TestClient(app) as client:
        monkeypatch.setattr(app.state.ollama_client, "tags", lambda: [])
        response = getattr(client, method)(
            path,
            json={"mode": "local", "model": candidate_model},
        )

    assert response.status_code == expected_status
    assert response.json()["detail"] == "本地模型不可用，请手动将该业务切换为 Mock 模式后重试"
    assert candidate_model not in response.text


@pytest.mark.parametrize(
    ("method", "path", "expected_status"),
    [
        ("put", "/api/settings/features/call_assistant", 503),
        ("post", "/api/settings/features/call_assistant/test", 200),
    ],
)
def test_feature_local_upstream_unavailable_has_a_fixed_non_reflective_detail(
    tmp_path, monkeypatch, method, path, expected_status
):
    app = create_app(Settings(runtime_dir=tmp_path))
    upstream_detail = "upstream-model-and-body-must-not-leak"
    with TestClient(app) as client:
        monkeypatch.setattr(
            app.state.ollama_client,
            "tags",
            lambda: (_ for _ in ()).throw(ProviderUnavailable(upstream_detail)),
        )
        response = getattr(client, method)(path, json={"mode": "local"})

    assert response.status_code == expected_status
    assert response.json()["detail"] == "本地模型不可用，请手动将该业务切换为 Mock 模式后重试"
    assert upstream_detail not in response.text


def test_feature_local_test_success_has_a_stable_detail(tmp_path, monkeypatch):
    app = create_app(Settings(runtime_dir=tmp_path))
    with TestClient(app) as client:
        monkeypatch.setattr(
            app.state.ollama_client,
            "tags",
            lambda: [
                type("Installed", (), {"name": app.state.ollama_client.model})()
            ],
        )
        response = client.post(
            "/api/settings/features/call_assistant/test",
            json={"mode": "local"},
        )

    assert response.json() == {"available": True, "detail": "本地模型可用"}


def test_feature_settings_use_only_new_modes_in_audits(tmp_path):
    app = create_app(Settings(runtime_dir=tmp_path))
    with TestClient(app) as client:
        response = client.put(
            "/api/settings/features/document_review",
            json={"mode": "mock"},
        )
        with app.state.session_factory() as session:
            events = list(
                session.scalars(
                    select(AuditEvent).where(
                        AuditEvent.event_type == "settings.feature.updated"
                    )
                )
            )

    assert response.status_code == 200
    assert [event.mode for event in events] == ["mock"]


def test_feature_mode_update_payload_needs_only_mode_and_changes_one_feature(
    tmp_path,
):
    app = create_app(Settings(runtime_dir=tmp_path))
    with TestClient(app) as client:
        response = client.put(
            "/api/settings/features/knowledge",
            json={"mode": "mock"},
        )
        settings = client.get("/api/settings/features").json()

    assert response.status_code == 200
    assert {
        "feature": response.json()["feature"],
        "mode": response.json()["mode"],
    } == {"feature": "knowledge", "mode": "mock"}
    assert [(item["feature"], item["mode"]) for item in settings] == [
        ("call_assistant", "mock"),
        ("knowledge", "mock"),
        ("document_review", "mock"),
    ]
