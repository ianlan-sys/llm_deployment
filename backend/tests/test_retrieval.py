from sqlalchemy import select

from app.models import KnowledgeSource
from app.retrieval.hashing import HASH_DIMENSIONS, hash_text
from app.retrieval.numpy_index import NumpyVectorIndex
from app.retrieval import service as retrieval_module
from app.retrieval.service import RetrievalService
from app.seed import seed_demo_data


def test_hash_text_is_stable_and_l2_normalized():
    first = hash_text("核心交换机 VLAN")
    second = hash_text("核心交换机 VLAN")

    assert first.shape == (HASH_DIMENSIONS,)
    assert (first == second).all()
    assert round(float(first @ first), 6) == 1.0


def test_retrieval_respects_scope(db_session):
    seed_demo_data(db_session)
    expected_source_ids = set(
        db_session.scalars(select(KnowledgeSource.id).where(KnowledgeSource.scope == "核心节点A"))
    )

    results = RetrievalService(db_session).search("核心交换机 VLAN", scope="核心节点A", limit=5)

    assert results
    assert {result.source_id for result in results} <= expected_source_ids


def test_retrieval_returns_highest_scoring_matching_chunk(db_session):
    seed_demo_data(db_session)

    results = RetrievalService(db_session).search("VLAN 410 网关", scope="核心节点A", limit=1)

    assert len(results) == 1
    assert "VLAN 410" in results[0].text
    assert results[0].score > 0
    assert results[0].source_title
    assert results[0].source_updated_at.tzinfo is not None


def test_retrieval_uses_numpy_when_faiss_is_unavailable(db_session, monkeypatch):
    def unavailable_faiss():
        raise ImportError("FAISS is not installed")

    monkeypatch.setattr(retrieval_module, "FaissVectorIndex", unavailable_faiss)

    service = RetrievalService(db_session)

    assert isinstance(service.index, NumpyVectorIndex)
    assert service.engine_name == "numpy"
