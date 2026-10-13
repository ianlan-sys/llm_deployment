from datetime import datetime, timezone
from threading import Event, Thread

import numpy as np
import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.config import Settings
from app.main import create_app
from app.models import KnowledgeChunk, KnowledgeSource
from app.retrieval.hashing import HASH_DIMENSIONS
from app.services.knowledge_index import (
    IndexedChunk,
    KnowledgeIndexService,
    KnowledgeIndexSnapshot,
)


def _add_document(
    session: Session,
    document_id: str,
    *,
    status: str,
    content: str,
    scope: str = "全局",
) -> None:
    session.add(
        KnowledgeSource(
            id=document_id,
            title=f"{document_id} title",
            source_type="uploaded_document",
            scope=scope,
            content=content,
            status=status,
            category="网络",
            version="1.0",
            parse_warnings=[],
        )
    )
    session.add(
        KnowledgeChunk(
            id=f"{document_id}-chunk",
            source_id=document_id,
            content=content,
            location="正文",
            position=1,
            index_key=document_id,
        )
    )
    session.commit()


def _snapshot(chunk_id: str, text: str) -> KnowledgeIndexSnapshot:
    record = IndexedChunk(
        chunk_id=chunk_id,
        source_id=f"{chunk_id}-source",
        source_title=f"{chunk_id} title",
        source_updated_at=datetime(2026, 7, 24, tzinfo=timezone.utc),
        scope="全局",
        text=text,
        location="正文",
    )
    vector = np.ones((1, HASH_DIMENSIONS), dtype=np.float32)
    vector.setflags(write=False)
    return KnowledgeIndexSnapshot(
        records=(record,),
        vectors=vector,
        engine_name="numpy",
        built_at=datetime(2026, 7, 24, tzinfo=timezone.utc),
    )


def test_rebuild_indexes_only_active_documents_and_caps_retrieval_at_four(
    db_session,
):
    for number in range(6):
        _add_document(
            db_session,
            f"active-{number}",
            status="active",
            content=f"VLAN 410 active evidence {number}",
        )
    _add_document(
        db_session,
        "pending",
        status="pending_review",
        content="VLAN 410 pending secret",
    )
    _add_document(
        db_session,
        "archived",
        status="archived",
        content="VLAN 410 archived secret",
    )
    service = KnowledgeIndexService()

    snapshot = service.rebuild(db_session)
    results = service.search("VLAN 410 active evidence", None, limit=99)

    assert len(snapshot.records) == 6
    assert len(results) == 4
    assert {result.source_id for result in results} <= {
        f"active-{number}" for number in range(6)
    }
    assert not snapshot.vectors.flags.writeable
    with pytest.raises(ValueError, match="WRITEABLE"):
        snapshot.vectors.setflags(write=True)


def test_search_acquires_exactly_one_snapshot_for_vectors_and_metadata(
    monkeypatch,
):
    service = KnowledgeIndexService()
    first = _snapshot("first", "VLAN 410 first")
    second = _snapshot("second", "VLAN 410 second")
    acquired = []

    def alternating_snapshot():
        acquired.append(True)
        return first if len(acquired) == 1 else second

    monkeypatch.setattr(service, "_acquire_snapshot", alternating_snapshot)

    results = service.search("VLAN 410 first", None)

    assert len(acquired) == 1
    assert [result.chunk_id for result in results] == ["first"]
    assert results[0].text == "VLAN 410 first"


def test_failed_activation_commit_keeps_previous_snapshot_searchable():
    service = KnowledgeIndexService()
    previous = _snapshot("previous", "previous searchable marker")
    candidate = _snapshot("candidate", "candidate marker")
    service.activate(previous, lambda: None)

    with pytest.raises(RuntimeError, match="commit rejected"):
        service.activate(
            candidate,
            lambda: (_ for _ in ()).throw(RuntimeError("commit rejected")),
        )

    assert service.search("previous searchable marker", None)[0].chunk_id == "previous"


def test_search_cannot_observe_candidate_until_commit_returns():
    service = KnowledgeIndexService()
    service.activate(_snapshot("previous", "previous marker"), lambda: None)
    candidate = _snapshot("candidate", "candidate marker")
    commit_started = Event()
    allow_commit = Event()
    search_finished = Event()
    result_ids = []

    def commit():
        commit_started.set()
        assert allow_commit.wait(1)

    activation = Thread(target=lambda: service.activate(candidate, commit))
    activation.start()
    assert commit_started.wait(1)

    search = Thread(
        target=lambda: (
            result_ids.extend(
                result.chunk_id
                for result in service.search("candidate marker", None)
            ),
            search_finished.set(),
        )
    )
    search.start()
    assert not search_finished.wait(0.05)

    allow_commit.set()
    activation.join(timeout=1)
    search.join(timeout=1)

    assert not activation.is_alive()
    assert not search.is_alive()
    assert result_ids == ["candidate"]


def test_startup_rebuild_repairs_persisted_active_document_after_restart(
    tmp_path,
):
    settings = Settings(runtime_dir=tmp_path, auth_required=False)
    first_app = create_app(settings)
    with TestClient(first_app):
        with first_app.state.session_factory() as session:
            _add_document(
                session,
                "persisted-after-swap-crash",
                status="active",
                content="restart repair marker Z-91",
            )
        assert all(
            result.source_id != "persisted-after-swap-crash"
            for result in first_app.state.knowledge_index_service.search(
                "restart repair marker Z-91", None
            )
        )

    restarted_app = create_app(settings)
    with TestClient(restarted_app):
        results = restarted_app.state.knowledge_index_service.search(
            "restart repair marker Z-91", None
        )

    assert results[0].source_id == "persisted-after-swap-crash"
