import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app


def test_lifespan_closes_ollama_client_even_when_executor_shutdown_fails(tmp_path, monkeypatch):
    closed: list[bool] = []

    class CloseSpyOllamaClient:
        def __init__(self, **_: object):
            pass

        def close(self) -> None:
            closed.append(True)

    class FailingReviewExecutor:
        def __init__(self, *_: object):
            pass

        def shutdown(self) -> None:
            raise RuntimeError("executor shutdown failed")

    monkeypatch.setattr("app.main.OllamaClient", CloseSpyOllamaClient)
    monkeypatch.setattr("app.main.ReviewExecutor", FailingReviewExecutor)
    app = create_app(Settings(runtime_dir=tmp_path))

    with pytest.raises(RuntimeError, match="executor shutdown failed"):
        with TestClient(app):
            pass

    assert closed == [True]


def test_health_mode_tracks_call_assistant_feature_after_settings_update(tmp_path, monkeypatch):
    app = create_app(Settings(runtime_dir=tmp_path))
    with TestClient(app) as client:
        monkeypatch.setattr(
            app.state.ollama_client,
            "tags",
            lambda: [type("Installed", (), {"name": app.state.ollama_client.model})()],
        )
        client.put("/api/settings/features/call_assistant", json={"mode": "local"})
        response = client.get("/api/health")

    assert response.json() == {"status": "ok", "mode": "local", "version": "0.1.0"}


def test_health_reports_mock_mode(tmp_path):
    settings = Settings(runtime_dir=tmp_path, provider_mode="demo")
    with TestClient(create_app(settings)) as client:
        response = client.get("/api/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok", "mode": "mock", "version": "0.1.0"}


def test_health_uses_call_assistant_feature_mode(tmp_path):
    with TestClient(create_app(Settings(runtime_dir=tmp_path))) as client:
        response = client.get("/api/health")

    assert response.json() == {"status": "ok", "mode": "mock", "version": "0.1.0"}


def test_application_metadata_uses_required_title(tmp_path):
    app = create_app(Settings(runtime_dir=tmp_path))

    assert app.title == "本地化大模型应用平台"
