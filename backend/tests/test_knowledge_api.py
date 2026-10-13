from datetime import datetime, timezone
import json

from sqlalchemy import select
from fastapi.testclient import TestClient
import pytest
from pydantic import ValidationError

from app.config import Settings
from app.main import create_app
from app.models import AuditEvent
from app.providers.base import ProviderUnavailable, ProviderUsage
from app.providers.feature_router import FeatureProviderSnapshot
from app.providers.mock import MockProvider
from app.retrieval.index import SearchResult
from app.schemas import KnowledgeQueryResponse
from app.services.knowledge import KnowledgeService


DISCLAIMER = "模型仅提供配置查询与操作参考，不会直接下发配置；实际变更必须按既有审批流程由人工执行。"


class StaticIndexService:
    engine_name = "numpy"

    def __init__(self, score: float):
        self.result = SearchResult(
            chunk_id="retrieved-chunk",
            score=score,
            source_id="retrieved-source",
            source_title="Retrieved source title",
            source_updated_at=datetime(2026, 7, 20, tzinfo=timezone.utc),
            text="VLAN 410",
            location="网络参数",
        )

    def search(self, query: str, scope: str | None, limit: int = 4) -> list[SearchResult]:
        return [self.result]


class StreamingProvider:
    @property
    def last_usage(self):
        return ProviderUsage(input_tokens=None, output_tokens=None)

    def reset_usage(self):
        return None

    def stream_answer_with_context(self, question, chunks):
        yield "有依据的回答"


class UnavailableProvider(StreamingProvider):
    def stream_answer_with_context(self, question, chunks):
        raise ProviderUnavailable("sensitive provider detail")
        yield


class RecordingCoordinator:
    def stream(
        self, feature, operation, snapshot, iterator_factory, cancellation=None
    ):
        return iterator_factory()


def _snapshot(provider=None):
    return FeatureProviderSnapshot(
        feature="knowledge",
        mode="local",
        model="qwen3:1.7b-q4_K_M",
        provider=provider or StreamingProvider(),
    )


def _use_provider(app, provider, *, mode="mock", model=None):
    router = app.state.feature_provider_router
    desired = FeatureProviderSnapshot(
        feature="knowledge",
        mode=mode,
        model=model,
        provider=provider,
    )
    with router._lock:
        router._snapshots = router._snapshots | {
            "knowledge": desired
        }


def test_answered_response_requires_the_mandatory_disclaimer():
    with pytest.raises(ValidationError, match="disclaimer"):
        KnowledgeQueryResponse(
            status="answered",
            answer="回答",
            citations=[],
            retrieval_engine="numpy",
        )


def test_knowledge_query_refuses_without_evidence(tmp_path):
    with TestClient(create_app(Settings(runtime_dir=tmp_path))) as client:
        response = client.post("/api/knowledge/query", json={"question": "月球节点密码是什么", "scope": None})

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "no_evidence"
    assert body["answer"] == "知识库中暂无可靠依据，请调整关键词或资料范围。"
    assert body["citations"] == []
    assert body["retrieval_engine"] in {"numpy", "faiss"}


def test_knowledge_query_returns_only_citations_for_retrieved_evidence(tmp_path):
    app = create_app(Settings(runtime_dir=tmp_path))
    with TestClient(app) as client:
        _use_provider(app, MockProvider(delay_ms=0))
        response = client.post(
            "/api/knowledge/query",
            json={"question": "核心交换机 VLAN 如何划分", "scope": "核心节点A"},
        )

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "answered"
    assert body["citations"]
    assert all({"chunk_id", "source_id", "source_title", "source_updated_at", "score", "text", "location"} <= set(citation) for citation in body["citations"])
    assert all(citation["source_title"] and citation["source_updated_at"] and citation["text"] and citation["score"] >= 0 for citation in body["citations"])
    assert body["disclaimer"] == DISCLAIMER


def test_valid_provider_citation_is_reconstructed_from_retrieved_metadata(db_session):
    response = KnowledgeService(
        db_session,
        StaticIndexService(0.8),
        RecordingCoordinator(),
        _snapshot(),
    ).query("VLAN", None)

    citation = response.citations[0]
    assert citation.chunk_id == "retrieved-chunk"
    assert citation.source_id == "retrieved-source"
    assert citation.source_title == "Retrieved source title"
    assert citation.source_updated_at == datetime(2026, 7, 20, tzinfo=timezone.utc)
    assert citation.score == 0.8
    assert citation.text == "VLAN 410"
    assert citation.location == "网络参数"


def test_provider_answer_text_cannot_invent_server_citations(db_session):
    class InventingProvider(StreamingProvider):
        def stream_answer_with_context(self, question, chunks):
            yield "回答声称引用 unknown-chunk"

    service = KnowledgeService(
        db_session,
        StaticIndexService(0.8),
        RecordingCoordinator(),
        _snapshot(InventingProvider()),
    )

    response = service.query("VLAN", None)

    assert response.status == "answered"
    assert response.answer == "回答声称引用 unknown-chunk"
    assert [citation.chunk_id for citation in response.citations] == [
        "retrieved-chunk"
    ]


def test_evidence_threshold_accepts_exact_boundary_and_rejects_below_it(db_session):
    accepted = KnowledgeService(
        db_session,
        StaticIndexService(0.30),
        RecordingCoordinator(),
        _snapshot(),
        minimum_score=0.30,
    ).query("VLAN", None)
    rejected = KnowledgeService(
        db_session,
        StaticIndexService(0.299999),
        RecordingCoordinator(),
        _snapshot(),
        minimum_score=0.30,
    ).query("VLAN", None)

    assert accepted.status == "answered"
    assert rejected.status == "no_evidence"


def test_provider_unavailable_is_a_redacted_chinese_503_and_audited(tmp_path):
    question = "核心交换机 VLAN 如何划分"
    app = create_app(Settings(runtime_dir=tmp_path))
    with TestClient(app, raise_server_exceptions=False) as client:
        _use_provider(
            app,
            UnavailableProvider(),
            mode="local",
            model="qwen3:1.7b-q4_K_M",
        )
        response = client.post(
            "/api/knowledge/query",
            json={"question": question, "scope": "核心节点A"},
        )
        with app.state.session_factory() as session:
            events = list(session.scalars(select(AuditEvent)))

    assert response.status_code == 503
    assert response.json() == {
        "detail": "模型服务暂不可用，请重试或手动切换为 Mock 模式。"
    }
    assert [(event.event_type, event.result_status) for event in events] == [
        ("knowledge.query.failed", "failed")
    ]
    assert question not in json.dumps(
        [event.request_summary for event in events], ensure_ascii=False
    )
