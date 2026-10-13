import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Event, Lock, Thread
from time import monotonic, sleep

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session
from sqlalchemy.orm.state import InstanceState

from app.config import Settings
from app.main import create_app
from app.models import AuditEvent, Base, KnowledgeChunk, KnowledgeSource
from app.api import knowledge_documents
from app.services import knowledge_ingestion
from app.services.knowledge_ingestion import (
    KnowledgeIngestionExecutor,
    KnowledgeIngestionService,
    KnowledgeIndexingFailed,
)
from app.services.review_parser import ParsedDocument, ParsedSection


def _login(client) -> None:
    response = client.post(
        "/api/auth/login",
        json={"username": "demo", "password": "correct horse"},
    )
    assert response.status_code == 200


def _wait_for_status(client, document_id: str, expected: str) -> dict:
    deadline = monotonic() + 3
    while monotonic() < deadline:
        response = client.get(f"/api/knowledge-documents/{document_id}")
        assert response.status_code == 200
        body = response.json()
        if body["status"] == expected:
            return body
        sleep(0.02)
    raise AssertionError(f"document {document_id} did not reach {expected}")


def _search(client, text: str):
    return client.app.state.knowledge_index_service.search(
        text, None, limit=10
    )


def test_document_routes_require_authentication_and_same_origin(auth_client):
    upload = {
        "files": {"file": ("runbook.txt", b"safe text", "text/plain")},
        "data": {
            "title": "恢复手册",
            "category": "数据库、备份与恢复",
            "scope": "全局",
            "version": "1.0",
        },
    }
    assert auth_client.get("/api/knowledge-documents").status_code == 401
    assert auth_client.post("/api/knowledge-documents", **upload).status_code == 401

    _login(auth_client)
    response = auth_client.post(
        "/api/knowledge-documents",
        headers={"Origin": "http://attacker.invalid"},
        **upload,
    )

    assert response.status_code == 403
    assert response.json() == {"detail": "非法来源请求"}


def test_uploaded_document_requires_review_before_retrieval(auth_client):
    sentinel = "Database recovery uses checkpoint R-42."
    filename = "private-runbook-name.txt"
    _login(auth_client)

    response = auth_client.post(
        "/api/knowledge-documents",
        files={"file": (filename, sentinel.encode(), "text/plain")},
        data={
            "title": "数据库恢复手册",
            "category": "数据库、备份与恢复",
            "scope": "全局",
            "version": "1.0",
        },
    )

    assert response.status_code == 202
    upload_body = response.json()
    document_id = upload_body["id"]
    assert filename not in response.text
    assert sentinel not in response.text
    preview = _wait_for_status(auth_client, document_id, "pending_review")
    assert preview["chunks"][0]["content"] == sentinel
    assert all(
        result.source_id != document_id
        for result in _search(auth_client, "checkpoint R-42")
    )

    approved = auth_client.post(
        f"/api/knowledge-documents/{document_id}/approve"
    )

    assert approved.status_code == 200
    assert approved.json()["status"] == "active"
    assert approved.json()["reviewed_by"] == "demo"
    assert _search(auth_client, "checkpoint R-42")[0].source_id == document_id

    archived = auth_client.post(
        f"/api/knowledge-documents/{document_id}/archive"
    )
    assert archived.status_code == 200
    assert archived.json()["status"] == "archived"
    assert all(
        result.source_id != document_id
        for result in _search(auth_client, "checkpoint R-42")
    )


def test_failed_approval_build_keeps_old_snapshot_and_persists_redacted_failure(
    auth_client,
    monkeypatch,
):
    document_id = "failed-index-document"
    sensitive_error = "vector failure included SECRET-CONTENT"
    with auth_client.app.state.session_factory() as session:
        session.add(
            KnowledgeSource(
                id=document_id,
                title="索引失败资料",
                source_type="uploaded_document",
                scope="全局",
                content="never searchable failed marker",
                status="pending_review",
                category="网络",
                version="1.0",
                parse_warnings=[],
            )
        )
        session.add(
            KnowledgeChunk(
                id=f"{document_id}-chunk",
                source_id=document_id,
                content="never searchable failed marker",
                location="正文",
                position=1,
                index_key=document_id,
            )
        )
        session.commit()

    monkeypatch.setattr(
        auth_client.app.state.knowledge_index_service,
        "build_candidate",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            RuntimeError(sensitive_error)
        ),
    )

    with pytest.raises(KnowledgeIndexingFailed, match="资料索引失败"):
        auth_client.app.state.knowledge_ingestion_service.approve(
            document_id, "demo"
        )

    assert all(
        result.source_id != document_id
        for result in _search(
            auth_client, "never searchable failed marker"
        )
    )
    with auth_client.app.state.session_factory() as session:
        source = session.get(KnowledgeSource, document_id)
        events = list(
            session.scalars(
                select(AuditEvent).where(
                    AuditEvent.reference_id == document_id
                )
            )
        )
    assert source.status == "index_failed"
    assert source.error_summary == "资料索引失败，请重试审核。"
    assert [
        (
            event.event_type,
            event.result_status,
            event.request_summary,
        )
        for event in events
    ] == [
        (
            "knowledge_document.failed",
            "index_failed",
            "Knowledge document indexing failed",
        )
    ]
    assert sensitive_error not in json.dumps(
        [
            source.error_summary,
            *[event.request_summary for event in events],
        ],
        ensure_ascii=False,
    )


def test_approval_commit_then_error_compensates_active_without_swapping(
    auth_client,
    monkeypatch,
):
    document_id = "commit-uncertain-document"
    with auth_client.app.state.session_factory() as session:
        session.add(
            KnowledgeSource(
                id=document_id,
                title="提交不确定资料",
                source_type="uploaded_document",
                scope="全局",
                content="commit uncertain marker C-73",
                status="pending_review",
                category="网络",
                version="1.0",
                parse_warnings=[],
            )
        )
        session.add(
            KnowledgeChunk(
                id=f"{document_id}-chunk",
                source_id=document_id,
                content="commit uncertain marker C-73",
                location="正文",
                position=1,
                index_key=document_id,
            )
        )
        session.commit()

    index_service = auth_client.app.state.knowledge_index_service

    def commit_then_raise(_candidate, commit):
        commit()
        raise RuntimeError("driver reported uncertain commit")

    monkeypatch.setattr(index_service, "activate", commit_then_raise)

    with pytest.raises(KnowledgeIndexingFailed, match="资料索引失败"):
        auth_client.app.state.knowledge_ingestion_service.approve(
            document_id, "demo"
        )

    assert all(
        result.source_id != document_id
        for result in _search(
            auth_client, "commit uncertain marker C-73"
        )
    )
    with auth_client.app.state.session_factory() as session:
        source = session.get(KnowledgeSource, document_id)
        events = list(
            session.scalars(
                select(AuditEvent).where(
                    AuditEvent.reference_id == document_id
                )
            )
        )
    assert source.status == "index_failed"
    assert source.error_summary == "资料索引失败，请重试审核。"
    assert [
        (event.event_type, event.result_status)
        for event in events
    ] == [
        ("knowledge_document.failed", "index_failed"),
    ]


def test_archive_commit_then_error_reconciles_snapshot(
    auth_client,
    monkeypatch,
):
    document_id = "archive-commit-uncertain"
    marker = "archive uncertain marker A-19"
    with auth_client.app.state.session_factory() as session:
        session.add(
            KnowledgeSource(
                id=document_id,
                title="待归档资料",
                source_type="uploaded_document",
                scope="全局",
                content=marker,
                status="active",
                category="网络",
                version="1.0",
                parse_warnings=[],
            )
        )
        session.add(
            KnowledgeChunk(
                id=f"{document_id}-chunk",
                source_id=document_id,
                content=marker,
                location="正文",
                position=1,
                index_key=document_id,
            )
        )
        session.commit()
        auth_client.app.state.knowledge_index_service.rebuild(session)

    assert any(
        result.source_id == document_id
        for result in _search(auth_client, marker)
    )
    index_service = auth_client.app.state.knowledge_index_service
    real_activate = index_service.activate
    calls = 0

    def commit_then_raise_once(candidate, commit):
        nonlocal calls
        calls += 1
        if calls == 1:
            commit()
            raise RuntimeError("driver reported uncertain archive commit")
        return real_activate(candidate, commit)

    monkeypatch.setattr(index_service, "activate", commit_then_raise_once)

    auth_client.app.state.knowledge_ingestion_service.archive(
        document_id, "demo"
    )

    assert calls == 2
    assert all(
        result.source_id != document_id
        for result in _search(auth_client, marker)
    )
    with auth_client.app.state.session_factory() as session:
        source = session.get(KnowledgeSource, document_id)
        events = list(
            session.scalars(
                select(AuditEvent).where(
                    AuditEvent.reference_id == document_id
                )
            )
        )
    assert source.status == "archived"
    assert [
        (event.event_type, event.result_status)
        for event in events
    ] == [
        ("knowledge_document.archived", "archived"),
    ]


def test_reject_retry_and_parse_failure_states_are_enforced(auth_client):
    _login(auth_client)
    response = auth_client.post(
        "/api/knowledge-documents",
        files={"file": ("invalid.pdf", b"%PDF-not-a-document", "application/pdf")},
        data={
            "title": "损坏的演示手册",
            "category": "故障分级与应急处置",
            "scope": "全局",
            "version": "1.0",
        },
    )
    assert response.status_code == 202
    document_id = response.json()["id"]
    failed = _wait_for_status(auth_client, document_id, "parse_failed")
    assert failed["error_summary"] == "文档解析失败，请检查文件格式或内容后重试。"
    assert len(failed["error_summary"]) <= 500

    assert (
        auth_client.post(f"/api/knowledge-documents/{document_id}/approve").status_code
        == 409
    )
    retried = auth_client.post(f"/api/knowledge-documents/{document_id}/retry")
    assert retried.status_code == 202
    _wait_for_status(auth_client, document_id, "parse_failed")

    pending = auth_client.post(
        "/api/knowledge-documents",
        files={"file": ("reject.txt", b"review then reject", "text/plain")},
        data={
            "title": "待驳回手册",
            "category": "变更管理、维护窗口和回退方案",
            "scope": "全局",
            "version": "1.0",
        },
    )
    pending_id = pending.json()["id"]
    _wait_for_status(auth_client, pending_id, "pending_review")
    rejected = auth_client.post(
        f"/api/knowledge-documents/{pending_id}/reject"
    )
    assert rejected.status_code == 200
    assert rejected.json()["status"] == "rejected"
    assert rejected.json()["reviewed_by"] == "demo"
    assert (
        auth_client.post(f"/api/knowledge-documents/{pending_id}/retry").status_code
        == 409
    )


def test_upload_limits_use_actual_bytes_and_require_matching_supported_type(
    auth_client,
):
    _login(auth_client)
    auth_client.app.state.settings.max_upload_bytes = 8
    fields = {
        "title": "边界测试",
        "category": "账号权限与安全基线",
        "scope": "全局",
        "version": "1.0",
    }

    oversized = auth_client.post(
        "/api/knowledge-documents",
        files={"file": ("tiny.txt", b"123456789", "text/plain")},
        data=fields,
    )
    mismatch = auth_client.post(
        "/api/knowledge-documents",
        files={"file": ("tiny.txt", b"1234", "application/pdf")},
        data=fields,
    )
    unsupported = auth_client.post(
        "/api/knowledge-documents",
        files={"file": ("tiny.csv", b"1234", "text/csv")},
        data=fields,
    )

    assert oversized.status_code == 413
    assert mismatch.status_code == 415
    assert unsupported.status_code == 415


def test_document_audits_use_fixed_summaries_without_filename_or_content(
    auth_client,
):
    filename = "secret-name.txt"
    content = "sensitive sentinel content"
    _login(auth_client)
    response = auth_client.post(
        "/api/knowledge-documents",
        files={"file": (filename, content.encode(), "text/plain")},
        data={
            "title": "审计脱敏测试",
            "category": "Linux/Windows 服务器巡检",
            "scope": "全局",
            "version": "1.0",
        },
    )
    document_id = response.json()["id"]
    _wait_for_status(auth_client, document_id, "pending_review")
    auth_client.post(f"/api/knowledge-documents/{document_id}/approve")

    with auth_client.app.state.session_factory() as session:
        events = list(
            session.scalars(
                select(AuditEvent).where(AuditEvent.reference_id == document_id)
            )
        )

    summaries = {event.request_summary for event in events}
    assert {"Knowledge document uploaded", "Knowledge document parsed", "Knowledge document approved"} <= summaries
    serialized = json.dumps(
        [
            {
                "summary": event.request_summary,
                "status": event.result_status,
                "reference_id": event.reference_id,
            }
            for event in events
        ]
    )
    assert filename not in serialized
    assert content not in serialized


def test_list_can_filter_by_status_without_returning_document_content(auth_client):
    _login(auth_client)
    response = auth_client.get("/api/knowledge-documents", params={"status": "active"})

    assert response.status_code == 200
    assert response.json()
    assert all(item["status"] == "active" for item in response.json())
    assert all("chunks" not in item and "content" not in item for item in response.json())
    assert all("original_filename" not in item for item in response.json())


@pytest.mark.parametrize("terminal_status", ["rejected", "archived"])
def test_stale_parse_completion_cannot_overwrite_rejection_or_archive(
    auth_client,
    tmp_path,
    monkeypatch,
    terminal_status,
):
    path = tmp_path / "stale.txt"
    path.write_text("stale parser output", encoding="utf-8")
    document_id = f"stale-parse-{terminal_status}"
    with auth_client.app.state.session_factory() as session:
        session.add(
            KnowledgeSource(
                id=document_id,
                title="并发状态测试",
                source_type="uploaded_document",
                scope="全局",
                content="",
                status="uploaded",
                category="故障分级与应急处置",
                version="1.0",
                file_path=str(path),
                media_type="text/plain",
                file_size=path.stat().st_size,
                parse_warnings=[],
            )
        )
        session.commit()

    def finish_after_terminal_transition(*_args, **_kwargs):
        with auth_client.app.state.session_factory() as session:
            source = session.get(KnowledgeSource, document_id)
            source.status = terminal_status
            session.commit()
        return ParsedDocument(
            sections=[ParsedSection(title="正文", content="stale parser output")]
        )

    monkeypatch.setattr(
        knowledge_ingestion,
        "parse_document",
        finish_after_terminal_transition,
    )

    completed = auth_client.app.state.knowledge_ingestion_service.parse(document_id)

    assert completed is False
    with auth_client.app.state.session_factory() as session:
        assert session.get(KnowledgeSource, document_id).status == terminal_status
        assert list(
            session.scalars(
                select(KnowledgeChunk).where(
                    KnowledgeChunk.source_id == document_id
                )
            )
        ) == []
    assert KnowledgeIngestionExecutor.max_workers <= 2


def test_shutdown_recovers_a_real_third_queued_upload(
    auth_client,
    tmp_path,
    monkeypatch,
):
    started = 0
    started_lock = Lock()
    two_started = Event()
    release = Event()
    document_ids = [f"queued-cancel-{index}" for index in range(3)]
    with auth_client.app.state.session_factory() as session:
        for document_id in document_ids:
            path = tmp_path / f"{document_id}.txt"
            path.write_text(document_id, encoding="utf-8")
            session.add(
                KnowledgeSource(
                    id=document_id,
                    title=document_id,
                    source_type="uploaded_document",
                    scope="全局",
                    content="",
                    status="uploaded",
                    category="故障分级与应急处置",
                    version="1.0",
                    file_path=str(path),
                    media_type="text/plain",
                    file_size=path.stat().st_size,
                    parse_warnings=[],
                )
            )
        session.commit()

    def blocked_parse(*_args, **_kwargs):
        nonlocal started
        with started_lock:
            started += 1
            if started == 2:
                two_started.set()
        assert release.wait(3)
        return ParsedDocument(
            sections=[ParsedSection(title="正文", content="completed")]
        )

    monkeypatch.setattr(knowledge_ingestion, "parse_document", blocked_parse)
    service = KnowledgeIngestionService(auth_client.app.state.session_factory)
    executor = KnowledgeIngestionExecutor(service)
    for document_id in document_ids:
        executor.submit(document_id)
    assert two_started.wait(2)

    shutdown = Thread(target=executor.shutdown)
    shutdown.start()
    try:
        deadline = monotonic() + 2
        queued_status = None
        while monotonic() < deadline:
            with auth_client.app.state.session_factory() as session:
                queued_status = session.get(
                    KnowledgeSource, document_ids[2]
                ).status
            if queued_status == "parse_failed":
                break
            sleep(0.02)
        assert queued_status == "parse_failed"
        with auth_client.app.state.session_factory() as session:
            events = list(
                session.scalars(
                    select(AuditEvent).where(
                        AuditEvent.reference_id == document_ids[2]
                    )
                )
            )
        assert [
            (event.request_summary, event.result_status) for event in events
        ] == [("Knowledge document failed", "parse_failed")]
    finally:
        release.set()
        shutdown.join(3)
    assert not shutdown.is_alive()


def test_restart_recovers_uploaded_and_parsing_once_without_terminal_overwrite(
    tmp_path,
):
    database_url = f"sqlite:///{tmp_path / 'recovery.db'}"
    engine = create_engine(
        database_url,
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(engine)
    statuses = {
        "restart-uploaded": "uploaded",
        "restart-parsing": "parsing",
        "restart-pending": "pending_review",
        "restart-rejected": "rejected",
        "restart-archived": "archived",
        "restart-active": "active",
    }
    with Session(engine) as session:
        for document_id, document_status in statuses.items():
            session.add(
                KnowledgeSource(
                    id=document_id,
                    title=document_id,
                    source_type="uploaded_document",
                    scope="全局",
                    content="",
                    status=document_status,
                    category="故障分级与应急处置",
                    version="1.0",
                    parse_warnings=[],
                )
            )
        session.commit()
    engine.dispose()

    for _ in range(2):
        with TestClient(
            create_app(
                Settings(runtime_dir=tmp_path, database_url=database_url)
            )
        ) as client:
            assert client.get("/api/health").status_code == 200

    engine = create_engine(
        database_url,
        connect_args={"check_same_thread": False},
    )
    with Session(engine) as session:
        actual = {
            document_id: session.get(KnowledgeSource, document_id).status
            for document_id in statuses
        }
        recovery_events = list(
            session.scalars(
                select(AuditEvent).where(
                    AuditEvent.reference_id.in_(
                        ("restart-uploaded", "restart-parsing")
                    )
                )
            )
        )
    engine.dispose()

    assert actual == statuses | {
        "restart-uploaded": "parse_failed",
        "restart-parsing": "parse_failed",
    }
    assert sorted(event.reference_id for event in recovery_events) == [
        "restart-parsing",
        "restart-uploaded",
    ]
    assert all(
        event.request_summary == "Knowledge document failed"
        and event.result_status == "parse_failed"
        for event in recovery_events
    )


@pytest.mark.parametrize(
    ("action_name", "initial_status", "with_chunk"),
    [
        ("approve", "pending_review", True),
        ("reject", "pending_review", False),
        ("archive", "active", False),
    ],
)
def test_review_state_rolls_back_when_audit_add_fails(
    auth_client,
    monkeypatch,
    action_name,
    initial_status,
    with_chunk,
):
    document_id = f"atomic-{action_name}"
    with auth_client.app.state.session_factory() as session:
        session.add(
            KnowledgeSource(
                id=document_id,
                title=document_id,
                source_type="uploaded_document",
                scope="全局",
                content="parsed",
                status=initial_status,
                category="故障分级与应急处置",
                version="1.0",
                parse_warnings=[],
            )
        )
        if with_chunk:
            session.add(
                KnowledgeChunk(
                    id=f"{document_id}-chunk",
                    source_id=document_id,
                    content="parsed",
                    location="正文",
                    position=1,
                    index_key=document_id,
                )
            )
        session.commit()

    original_add = Session.add

    def fail_audit_add(session, instance, *args, **kwargs):
        if isinstance(instance, AuditEvent):
            raise RuntimeError("injected audit add failure")
        return original_add(session, instance, *args, **kwargs)

    monkeypatch.setattr(Session, "add", fail_audit_add)
    action = getattr(
        auth_client.app.state.knowledge_ingestion_service,
        action_name,
    )

    with pytest.raises(RuntimeError, match="audit add"):
        action(document_id, "demo")

    with auth_client.app.state.session_factory() as session:
        source = session.get(KnowledgeSource, document_id)
        assert source.status == initial_status
        assert source.reviewed_by is None


@pytest.mark.parametrize(
    ("filename", "content", "media_type", "expected_status"),
    [
        ("atomic-success.txt", b"parsed content", "text/plain", "parsing"),
        (
            "atomic-failure.pdf",
            b"%PDF-invalid",
            "application/pdf",
            "parsing",
        ),
    ],
)
def test_parse_state_and_chunks_roll_back_when_audit_add_fails(
    auth_client,
    tmp_path,
    monkeypatch,
    filename,
    content,
    media_type,
    expected_status,
):
    document_id = f"atomic-parse-{Path(filename).stem}"
    path = tmp_path / filename
    path.write_bytes(content)
    with auth_client.app.state.session_factory() as session:
        session.add(
            KnowledgeSource(
                id=document_id,
                title=document_id,
                source_type="uploaded_document",
                scope="全局",
                content="",
                status="uploaded",
                category="故障分级与应急处置",
                version="1.0",
                file_path=str(path),
                media_type=media_type,
                file_size=len(content),
                parse_warnings=[],
            )
        )
        session.commit()

    original_add = Session.add

    def fail_audit_add(session, instance, *args, **kwargs):
        if isinstance(instance, AuditEvent):
            raise RuntimeError("injected audit add failure")
        return original_add(session, instance, *args, **kwargs)

    monkeypatch.setattr(Session, "add", fail_audit_add)

    with pytest.raises(RuntimeError, match="audit add"):
        auth_client.app.state.knowledge_ingestion_service.parse(document_id)

    with auth_client.app.state.session_factory() as session:
        source = session.get(KnowledgeSource, document_id)
        assert source.status == expected_status
        assert list(
            session.scalars(
                select(KnowledgeChunk).where(
                    KnowledgeChunk.source_id == document_id
                )
            )
        ) == []


@pytest.mark.parametrize("failure_point", ["add", "flush", "commit"])
def test_upload_audit_failure_leaves_no_row_file_or_parser_submission(
    auth_client,
    monkeypatch,
    failure_point,
):
    _login(auth_client)
    submitted: list[str] = []
    monkeypatch.setattr(
        auth_client.app.state.knowledge_ingestion_executor,
        "submit",
        submitted.append,
    )
    original_add = Session.add
    original_flush = Session.flush

    def fail_add(session, instance, *args, **kwargs):
        if isinstance(instance, AuditEvent):
            raise RuntimeError("injected audit add failure")
        return original_add(session, instance, *args, **kwargs)

    def fail_flush(session, *args, **kwargs):
        if any(isinstance(instance, AuditEvent) for instance in session.new):
            raise RuntimeError("injected audit flush failure")
        return original_flush(session, *args, **kwargs)

    def fail_commit(_session, *_args, **_kwargs):
        raise RuntimeError("injected audit commit failure")

    if failure_point == "add":
        monkeypatch.setattr(Session, "add", fail_add)
    elif failure_point == "flush":
        monkeypatch.setattr(Session, "flush", fail_flush)
    else:
        monkeypatch.setattr(Session, "commit", fail_commit)

    response = auth_client.post(
        "/api/knowledge-documents",
        files={"file": ("atomic-upload.txt", b"atomic upload", "text/plain")},
        data={
            "title": "原子上传",
            "category": "故障分级与应急处置",
            "scope": "全局",
            "version": "1.0",
        },
    )

    assert response.status_code == 500
    assert submitted == []
    with auth_client.app.state.session_factory() as session:
        assert (
            session.scalar(
                select(KnowledgeSource).where(
                    KnowledgeSource.title == "原子上传"
                )
            )
            is None
        )
    upload_dir = auth_client.app.state.settings.runtime_dir / "knowledge_uploads"
    assert not upload_dir.exists() or list(upload_dir.iterdir()) == []


def test_upload_refresh_failure_leaves_no_row_audit_file_or_parser_submission(
    auth_client,
    monkeypatch,
):
    _login(auth_client)
    submitted: list[str] = []
    monkeypatch.setattr(
        auth_client.app.state.knowledge_ingestion_executor,
        "submit",
        submitted.append,
    )
    original_refresh = Session.refresh

    def fail_source_refresh(session, instance, *args, **kwargs):
        if isinstance(instance, KnowledgeSource):
            raise RuntimeError("injected source refresh failure")
        return original_refresh(session, instance, *args, **kwargs)

    monkeypatch.setattr(Session, "refresh", fail_source_refresh)
    response = auth_client.post(
        "/api/knowledge-documents",
        files={"file": ("refresh.txt", b"refresh failure", "text/plain")},
        data={
            "title": "刷新失败上传",
            "category": "故障分级与应急处置",
            "scope": "全局",
            "version": "1.0",
        },
    )

    assert response.status_code == 500
    assert submitted == []
    with auth_client.app.state.session_factory() as session:
        assert (
            session.scalar(
                select(KnowledgeSource).where(
                    KnowledgeSource.title == "刷新失败上传"
                )
            )
            is None
        )
        assert list(
            session.scalars(
                select(AuditEvent).where(
                    AuditEvent.event_type == "knowledge_document.uploaded"
                )
            )
        ) == []
    upload_dir = auth_client.app.state.settings.runtime_dir / "knowledge_uploads"
    assert not upload_dir.exists() or list(upload_dir.iterdir()) == []


def test_upload_response_construction_failure_leaves_no_artifacts_or_job(
    auth_client,
    monkeypatch,
):
    _login(auth_client)
    submitted: list[str] = []
    monkeypatch.setattr(
        auth_client.app.state.knowledge_ingestion_executor,
        "submit",
        submitted.append,
    )

    def fail_response_construction(_source):
        raise RuntimeError("injected response construction failure")

    monkeypatch.setattr(
        knowledge_documents,
        "_summary",
        fail_response_construction,
    )
    response = auth_client.post(
        "/api/knowledge-documents",
        files={"file": ("response.txt", b"response failure", "text/plain")},
        data={
            "title": "响应失败上传",
            "category": "故障分级与应急处置",
            "scope": "全局",
            "version": "1.0",
        },
    )

    assert response.status_code == 500
    assert submitted == []
    with auth_client.app.state.session_factory() as session:
        assert (
            session.scalar(
                select(KnowledgeSource).where(
                    KnowledgeSource.title == "响应失败上传"
                )
            )
            is None
        )
        assert list(
            session.scalars(
                select(AuditEvent).where(
                    AuditEvent.event_type == "knowledge_document.uploaded"
                )
            )
        ) == []
    upload_dir = auth_client.app.state.settings.runtime_dir / "knowledge_uploads"
    assert not upload_dir.exists() or list(upload_dir.iterdir()) == []


def test_post_commit_expired_orm_load_cannot_block_parser_submission(
    auth_client,
    monkeypatch,
):
    _login(auth_client)
    submitted: list[str] = []
    monkeypatch.setattr(
        auth_client.app.state.knowledge_ingestion_executor,
        "submit",
        submitted.append,
    )

    def fail_expired_load(*_args, **_kwargs):
        raise RuntimeError("post-commit ORM load is forbidden")

    monkeypatch.setattr(InstanceState, "_load_expired", fail_expired_load)
    response = auth_client.post(
        "/api/knowledge-documents",
        files={"file": ("expired.txt", b"expired id", "text/plain")},
        data={
            "title": "过期属性上传",
            "category": "故障分级与应急处置",
            "scope": "全局",
            "version": "1.0",
        },
    )

    assert response.status_code == 202
    assert submitted == [response.json()["id"]]


def test_submission_failure_recovery_uses_local_id_without_expired_orm_load(
    auth_client,
    monkeypatch,
):
    _login(auth_client)
    submitted: list[str] = []

    def fail_submit(document_id):
        submitted.append(document_id)
        raise RuntimeError("injected submission failure")

    def fail_expired_load(*_args, **_kwargs):
        raise RuntimeError("post-commit ORM load is forbidden")

    monkeypatch.setattr(
        auth_client.app.state.knowledge_ingestion_executor,
        "submit",
        fail_submit,
    )
    monkeypatch.setattr(InstanceState, "_load_expired", fail_expired_load)
    response = auth_client.post(
        "/api/knowledge-documents",
        files={"file": ("expired-recovery.txt", b"recover id", "text/plain")},
        data={
            "title": "过期属性补偿",
            "category": "故障分级与应急处置",
            "scope": "全局",
            "version": "1.0",
        },
    )

    assert response.status_code == 503
    assert len(submitted) == 1
    document_id = submitted[0]
    with auth_client.app.state.session_factory() as session:
        source = session.get(KnowledgeSource, document_id)
        events = list(
            session.scalars(
                select(AuditEvent).where(
                    AuditEvent.reference_id == document_id
                )
            )
        )
    assert source.status == "parse_failed"
    assert [
        (event.request_summary, event.result_status) for event in events
    ] == [
        ("Knowledge document uploaded", "uploaded"),
        ("Knowledge document failed", "parse_failed"),
    ]


def test_partial_file_write_failure_leaves_no_orphan_or_parser_submission(
    auth_client,
    monkeypatch,
):
    _login(auth_client)
    submitted: list[str] = []
    monkeypatch.setattr(
        auth_client.app.state.knowledge_ingestion_executor,
        "submit",
        submitted.append,
    )

    def partial_write(path, content):
        with path.open("wb") as stream:
            stream.write(content[:3])
        raise OSError("injected partial write failure")

    monkeypatch.setattr(Path, "write_bytes", partial_write)
    response = auth_client.post(
        "/api/knowledge-documents",
        files={"file": ("partial.txt", b"partial content", "text/plain")},
        data={
            "title": "部分写入",
            "category": "故障分级与应急处置",
            "scope": "全局",
            "version": "1.0",
        },
    )

    assert response.status_code == 500
    assert submitted == []
    upload_dir = auth_client.app.state.settings.runtime_dir / "knowledge_uploads"
    assert not upload_dir.exists() or list(upload_dir.iterdir()) == []


def test_authorized_approve_reject_race_has_one_state_and_one_audit(auth_client):
    _login(auth_client)
    upload = auth_client.post(
        "/api/knowledge-documents",
        files={"file": ("race.txt", b"authorized race", "text/plain")},
        data={
            "title": "授权竞态",
            "category": "故障分级与应急处置",
            "scope": "全局",
            "version": "1.0",
        },
    )
    document_id = upload.json()["id"]
    _wait_for_status(auth_client, document_id, "pending_review")

    with ThreadPoolExecutor(max_workers=2) as pool:
        responses = list(
            pool.map(
                lambda action: auth_client.post(
                    f"/api/knowledge-documents/{document_id}/{action}"
                ),
                ("approve", "reject"),
            )
        )

    assert sorted(response.status_code for response in responses) == [200, 409]
    detail = auth_client.get(
        f"/api/knowledge-documents/{document_id}"
    ).json()
    assert detail["status"] in {"active", "rejected"}
    assert detail["reviewed_by"] == "demo"
    with auth_client.app.state.session_factory() as session:
        review_events = list(
            session.scalars(
                select(AuditEvent).where(
                    AuditEvent.reference_id == document_id,
                    AuditEvent.event_type.in_(
                        (
                            "knowledge_document.approved",
                            "knowledge_document.rejected",
                        )
                    ),
                )
            )
        )
    assert len(review_events) == 1
