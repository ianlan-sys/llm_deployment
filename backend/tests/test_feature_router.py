from threading import Event, Thread

import httpx
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from app.models import Base
from app.providers.feature_router import FeatureProviderRouter
from app.providers.base import ProviderUnavailable
from app.providers.mock import MockProvider
from app.providers.ollama import OllamaClient
from app.providers.openai_compatible import OpenAICompatibleProvider


MODEL = "qwen3:1.7b-q4_K_M"


@pytest.fixture
def ollama_client():
    def responder(request: httpx.Request) -> httpx.Response:
        payloads = {
            "/api/version": {"version": "0.32.1"},
            "/api/tags": {"models": [{"name": MODEL, "size": 1}]},
            "/api/ps": {"models": []},
        }
        return httpx.Response(200, json=payloads[request.url.path])

    client = OllamaClient(
        "http://ollama:11434",
        MODEL,
        client=httpx.Client(transport=httpx.MockTransport(responder)),
    )
    yield client
    client.close()


def test_feature_defaults_and_independent_publication(db_session, ollama_client):
    router = FeatureProviderRouter(db_session.bind, ollama_client)
    router.load(db_session)

    assert router.snapshot("call_assistant").mode == "mock"
    assert router.snapshot("knowledge").mode == "local"
    assert router.snapshot("document_review").mode == "mock"

    router.configure_and_persist(db_session, "call_assistant", "local")

    assert router.snapshot("call_assistant").mode == "local"
    assert router.snapshot("document_review").mode == "mock"


def test_failed_commit_preserves_the_previously_published_feature_snapshot(
    tmp_path, ollama_client, monkeypatch
):
    engine = create_engine(f"sqlite:///{tmp_path / 'feature-router.db'}")
    Base.metadata.create_all(engine)
    router = FeatureProviderRouter(engine, ollama_client)
    before = router.snapshot("call_assistant")

    with Session(engine) as session:
        monkeypatch.setattr(
            session,
            "commit",
            lambda: (_ for _ in ()).throw(RuntimeError("commit failed")),
        )
        with pytest.raises(RuntimeError, match="commit failed"):
            router.configure_and_persist(session, "call_assistant", "local")

    assert router.snapshot("call_assistant") is before
    engine.dispose()


def test_local_update_with_missing_model_uses_the_fixed_unavailable_detail(
    db_session, ollama_client, monkeypatch
):
    router = FeatureProviderRouter(db_session.bind, ollama_client)
    monkeypatch.setattr(ollama_client, "model", "")

    with pytest.raises(ProviderUnavailable) as raised:
        router.configure_and_persist(db_session, "call_assistant", "local")

    assert str(raised.value) == "本地模型不可用，请手动将该业务切换为 Mock 模式后重试"


def test_feature_snapshot_keeps_metadata_and_provider_identity_consistent_under_concurrency(
    tmp_path, ollama_client
):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'feature-router-concurrent.db'}",
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine)
    router = FeatureProviderRouter(engine, ollama_client)
    finished = Event()
    mismatches = []
    writer_errors = []
    provider_metadata = {}

    def writer():
        try:
            for index in range(30):
                with factory() as session:
                    router.configure_and_persist(
                        session,
                        "call_assistant",
                        "local" if index % 2 else "mock",
                    )
        except BaseException as exc:
            writer_errors.append(exc)
        finally:
            finished.set()

    worker = Thread(target=writer, daemon=True)
    worker.start()
    while not finished.is_set():
        snapshot = router.snapshot("call_assistant")
        signature = (
            snapshot.feature,
            snapshot.mode,
            snapshot.model,
            type(snapshot.provider),
        )
        provider_metadata.setdefault(snapshot.provider, set()).add(signature)
        if snapshot.feature != "call_assistant":
            mismatches.append(snapshot)
        if snapshot.mode == "mock" and not isinstance(snapshot.provider, MockProvider):
            mismatches.append(snapshot)
        if snapshot.mode == "local" and not isinstance(
            snapshot.provider, OpenAICompatibleProvider
        ):
            mismatches.append(snapshot)
        if snapshot.mode == "local" and snapshot.model != MODEL:
            mismatches.append(snapshot)
    worker.join(timeout=2)

    assert not worker.is_alive()
    assert writer_errors == []
    assert mismatches == []
    assert provider_metadata
    assert all(len(signatures) == 1 for signatures in provider_metadata.values())
    engine.dispose()
