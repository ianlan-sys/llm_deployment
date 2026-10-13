import json
from threading import Event, Thread
from threading import enumerate as enumerate_threads

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from app.config import Settings
from app.main import create_app
from app.models import ModelRequestMetric


def test_monitoring_live_returns_controlled_status_counters_and_features(
    tmp_path, monkeypatch
):
    app = create_app(Settings(runtime_dir=tmp_path))
    with TestClient(app) as client:
        monkeypatch.setattr(
            app.state.ollama_client,
            "model_status",
            lambda: type(
                "Health",
                (),
                {
                    "available": True,
                    "installed": True,
                    "loaded": False,
                    "version": "0.32.1",
                    "model": "API_KEY_SENTINEL",
                    "size_bytes": 1_400_000_000,
                },
            )(),
        )
        monkeypatch.setattr(
            app.state.inference_coordinator,
            "live_state",
            lambda: type(
                "Inference",
                (),
                {
                    "running": 1,
                    "queued": 2,
                    "concurrency": 1,
                    "max_queue": 3,
                },
            )(),
        )

        response = client.get("/api/monitoring/live")

    assert response.status_code == 200
    assert response.json() == {
        "sampling_status": "idle",
        "ollama": {
            "available": True,
            "installed": True,
            "loaded": False,
            "display_label": "Qwen3 1.7B Q4",
            "version": "0.32.1",
            "size_bytes": 1_400_000_000,
        },
        "inference": {
            "running": 1,
            "queued": 2,
            "concurrency": 1,
            "max_queue": 3,
        },
        "features": [
            {"feature": "call_assistant", "mode": "mock"},
            {"feature": "knowledge", "mode": "local"},
            {"feature": "document_review", "mode": "mock"},
        ],
    }
    serialized = response.text
    assert "API_KEY_SENTINEL" not in serialized


@pytest.mark.parametrize(
    ("version", "size_bytes"),
    [
        ("PASSWORD_SENTINEL http://internal-provider", 1),
        ("0.32.1", -1),
        ("0.32.1", 10_995_116_277_761),
        ("0.32.1", "API_KEY_SENTINEL"),
    ],
)
def test_monitoring_live_allowlists_display_metadata(
    tmp_path, monkeypatch, version, size_bytes
):
    app = create_app(Settings(runtime_dir=tmp_path))
    with TestClient(app) as client:
        monkeypatch.setattr(
            app.state.ollama_client,
            "model_status",
            lambda: type(
                "Health",
                (),
                {
                    "available": True,
                    "installed": True,
                    "loaded": True,
                    "version": version,
                    "model": "MODEL_SENTINEL",
                    "size_bytes": size_bytes,
                },
            )(),
        )

        response = client.get("/api/monitoring/live")

    assert response.status_code == 200
    assert response.json()["ollama"] == {
        "available": True,
        "installed": True,
        "loaded": True,
        "display_label": "Qwen3 1.7B Q4",
        "version": version if version == "0.32.1" else None,
        "size_bytes": (
            size_bytes
            if isinstance(size_bytes, int)
            and 0 <= size_bytes <= 10_995_116_277_760
            else None
        ),
    }
    serialized = response.text
    for sentinel in (
        "PASSWORD_SENTINEL",
        "internal-provider",
        "API_KEY_SENTINEL",
        "MODEL_SENTINEL",
    ):
        assert sentinel not in serialized


def test_monitoring_live_redacts_ollama_exception(tmp_path, monkeypatch):
    app = create_app(Settings(runtime_dir=tmp_path))
    with TestClient(app) as client:
        monkeypatch.setattr(
            app.state.ollama_client,
            "model_status",
            lambda: (_ for _ in ()).throw(
                RuntimeError(
                    "RAW_PROVIDER_ERROR password=PASSWORD_SENTINEL "
                    "cookie=COOKIE_SENTINEL http://internal-provider"
                )
            ),
        )

        response = client.get("/api/monitoring/live")

    assert response.status_code == 200
    assert response.json()["ollama"] == {
        "available": False,
        "installed": False,
        "loaded": False,
        "display_label": "Qwen3 1.7B Q4",
        "version": None,
        "size_bytes": None,
    }
    serialized = json.dumps(response.json())
    for sentinel in (
        "RAW_PROVIDER_ERROR",
        "PASSWORD_SENTINEL",
        "COOKIE_SENTINEL",
        "internal-provider",
    ):
        assert sentinel not in serialized


def test_monitoring_history_defaults_to_24_hours_and_validates_range(tmp_path):
    app = create_app(Settings(runtime_dir=tmp_path))
    with TestClient(app, raise_server_exceptions=False) as client:
        response = client.get("/api/monitoring/history")
        too_large = client.get("/api/monitoring/history?hours=25")

    assert response.status_code == 200
    assert response.json()["hours"] == 24
    assert response.json()["total_requests"] == 0
    assert too_large.status_code == 422


def test_monitoring_history_api_discards_malformed_secret_rows(tmp_path):
    secret = "PASSWORD_SENTINEL http://internal-provider API_KEY_SENTINEL"
    app = create_app(Settings(runtime_dir=tmp_path))
    with TestClient(app) as client:
        with app.state.session_factory() as session:
            session.add(
                ModelRequestMetric(
                    feature=secret,
                    mode=secret,
                    model=secret,
                    request_id="malformed-api-row",
                    operation=secret,
                    queue_wait_ms=0,
                    elapsed_ms=1,
                    result_status=secret,
                )
            )
            session.execute(
                text(
                    "INSERT INTO resource_samples "
                    "(id, cpu_percent, memory_percent, disk_percent, "
                    "ollama_available, model_loaded, running_count, "
                    "queued_count, created_at) "
                    "VALUES "
                    "('malformed-api-resource', :cpu, 20, 30, "
                    "0, 0, 0, 0, CURRENT_TIMESTAMP)"
                ),
                {"cpu": secret},
            )
            session.commit()

        response = client.get("/api/monitoring/history")

    assert response.status_code == 200
    assert response.json()["total_requests"] == 0
    assert "PASSWORD_SENTINEL" not in response.text
    assert "internal-provider" not in response.text
    assert "API_KEY_SENTINEL" not in response.text


def test_lifespan_stops_resource_sampler_without_thread_leak(tmp_path):
    app = create_app(Settings(runtime_dir=tmp_path))

    with TestClient(app):
        assert len(
            [
                thread
                for thread in enumerate_threads()
                if thread.name == "resource-sampler"
            ]
        ) == 1

    assert [
        thread
        for thread in enumerate_threads()
        if thread.name == "resource-sampler"
    ] == []


def test_lifespan_startup_failure_does_not_leak_resource_sampler(
    tmp_path, monkeypatch
):
    class FailingReviewExecutor:
        def __init__(self, *_):
            raise RuntimeError("review startup failed")

    monkeypatch.setattr("app.main.ReviewExecutor", FailingReviewExecutor)
    app = create_app(Settings(runtime_dir=tmp_path))

    with pytest.raises(RuntimeError, match="review startup failed"):
        with TestClient(app):
            pass

    assert [
        thread
        for thread in enumerate_threads()
        if thread.name == "resource-sampler"
    ] == []


def test_lifespan_registers_sampler_cleanup_before_start(tmp_path, monkeypatch):
    stopped = []

    class FailingResourceSampler:
        status = "sampling_error"

        def __init__(self, *_, **__):
            pass

        def start(self):
            raise RuntimeError("sampler startup failed")

        def stop(self):
            stopped.append(True)

    monkeypatch.setattr(
        "app.main.ResourceSampler",
        FailingResourceSampler,
    )
    app = create_app(Settings(runtime_dir=tmp_path))

    with pytest.raises(RuntimeError, match="sampler startup failed"):
        with TestClient(app):
            pass

    assert stopped == [True]


def test_lifespan_waits_for_active_sample_before_disposing_dependencies(
    tmp_path, monkeypatch
):
    from app.services.monitoring import ResourceSampler

    sampling = Event()
    release = Event()

    class FastResourceSampler(ResourceSampler):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs, interval_seconds=0.01)

    monkeypatch.setattr("app.main.ResourceSampler", FastResourceSampler)
    app = create_app(Settings(runtime_dir=tmp_path))

    def blocked_status():
        sampling.set()
        release.wait()
        return type(
            "Health",
            (),
            {"available": True, "loaded": True},
        )()

    monkeypatch.setattr(
        app.state.ollama_client,
        "model_status",
        blocked_status,
    )
    client = TestClient(app)
    client.__enter__()
    exit_thread = Thread(
        target=lambda: client.__exit__(None, None, None)
    )
    try:
        assert sampling.wait(1)
        exit_thread.start()
        exit_thread.join(timeout=0.05)
        assert exit_thread.is_alive()
        release.set()
        exit_thread.join(timeout=1)
        assert not exit_thread.is_alive()
        assert [
            thread
            for thread in enumerate_threads()
            if thread.name == "resource-sampler"
        ] == []
    finally:
        release.set()
        if exit_thread.ident is None:
            exit_thread.start()
        exit_thread.join(timeout=1)
