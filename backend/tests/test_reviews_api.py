from io import StringIO
import csv
from pathlib import Path

from fastapi.testclient import TestClient
import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session
from sqlalchemy.sql.dml import Update
from time import monotonic, sleep

from app.config import Settings
from app.main import create_app
from app.models import AuditEvent, ReviewFinding, ReviewRule, ReviewTask
from app.services.review_executor import ReviewExecutor
from app.api.reviews import _human_review_label, _risk_label, _spreadsheet_safe


class _RecordingExecutor:
    def __init__(self):
        self.submitted = []
        self.snapshots = []
        self.cancelled = []

    def submit(self, task_id, snapshot=None):
        self.submitted.append(task_id)
        self.snapshots.append(snapshot)

    def cancel(self, task_id):
        self.cancelled.append(task_id)
        return True


def _client(tmp_path, raise_server_exceptions=True):
    app = create_app(Settings(runtime_dir=tmp_path))
    client = TestClient(app, raise_server_exceptions=raise_server_exceptions)
    client.__enter__()
    executor = _RecordingExecutor()
    app.state.review_executor = executor
    return client, executor


def test_rejects_oversized_upload(tmp_path, monkeypatch):
    client, _ = _client(tmp_path)
    try:
        monkeypatch.setattr(client.app.state.settings, "max_upload_bytes", 10)

        response = client.post(
            "/api/reviews",
            data={"document_type": "technical_proposal"},
            files={"file": ("a.txt", b"12345678901", "text/plain")},
        )

        assert response.status_code == 413
        assert response.json() == {"detail": "上传文件超过大小限制"}
    finally:
        client.__exit__(None, None, None)


def test_rejects_empty_unsupported_and_mismatched_uploads(tmp_path):
    client, _ = _client(tmp_path)
    try:
        empty = client.post(
            "/api/reviews",
            data={"document_type": "technical_proposal"},
            files={"file": ("a.txt", b"", "text/plain")},
        )
        unsupported = client.post(
            "/api/reviews",
            data={"document_type": "technical_proposal"},
            files={"file": ("a.rtf", b"text", "application/rtf")},
        )
        mismatch = client.post(
            "/api/reviews",
            data={"document_type": "technical_proposal"},
            files={"file": ("a.txt", b"text", "application/pdf")},
        )

        assert empty.status_code == 422
        assert unsupported.status_code == 415
        assert mismatch.status_code == 415
        assert empty.json() == {"detail": "上传文件不能为空"}
        assert unsupported.json() == {"detail": "仅支持 PDF、DOCX 和 UTF-8 TXT 文件"}
        assert mismatch.json() == {"detail": "仅支持 PDF、DOCX 和 UTF-8 TXT 文件"}
    finally:
        client.__exit__(None, None, None)


def test_rejects_spoofed_pdf_docx_and_non_utf8_text(tmp_path):
    client, executor = _client(tmp_path)
    try:
        responses = [
            client.post(
                "/api/reviews",
                data={"document_type": "technical_proposal"},
                files={"file": ("a.pdf", b"not a pdf", "application/pdf")},
            ),
            client.post(
                "/api/reviews",
                data={"document_type": "technical_proposal"},
                files={
                    "file": (
                        "a.docx",
                        b"not a docx",
                        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                    )
                },
            ),
            client.post(
                "/api/reviews",
                data={"document_type": "technical_proposal"},
                files={"file": ("a.txt", b"\xff\xfe", "text/plain")},
            ),
        ]

        assert [response.status_code for response in responses] == [415, 415, 415]
        assert [response.json()["detail"] for response in responses] == [
            "上传内容不是有效的 PDF 文件",
            "上传内容不是有效的 DOCX 文件",
            "TXT 文件必须使用 UTF-8 编码",
        ]
        assert executor.submitted == []
    finally:
        client.__exit__(None, None, None)


def test_create_list_and_get_review_task(tmp_path):
    client, executor = _client(tmp_path)
    try:
        created = client.post(
            "/api/reviews",
            data={"document_type": "technical_proposal"},
            files={
                "file": ("proposal.txt", "一、项目范围\n正文".encode(), "text/plain")
            },
        )

        assert created.status_code == 202
        body = created.json()
        assert body["document_filename"] == "proposal.txt"
        assert body["status"] == "queued"
        assert body["progress"] == 0
        assert executor.submitted == [body["id"]]
        assert client.get("/api/reviews").json()[0]["id"] == body["id"]
        assert client.get(f"/api/reviews/{body['id']}").json()["findings"] == []
    finally:
        client.__exit__(None, None, None)


def test_retry_reuses_terminal_task_and_clears_old_results(tmp_path):
    client, executor = _client(tmp_path)
    try:
        with client.app.state.session_factory() as session:
            rule = session.scalar(select(ReviewRule).limit(1))
            task = ReviewTask(
                document_filename="proposal.txt",
                document_type=rule.document_type,
                file_path=str(tmp_path / "proposal.txt"),
                status="failed",
                stage="reviewing",
                progress=40,
                error_summary="malformed output",
            )
            session.add(task)
            session.flush()
            session.add(
                ReviewFinding(
                    review_task_id=task.id,
                    review_rule_id=rule.id,
                    risk="high",
                    description="old",
                    location="section",
                    excerpt="old",
                    recommendation="fix",
                )
            )
            session.commit()
            task_id = task.id

        response = client.post(f"/api/reviews/{task_id}/retry")

        assert response.status_code == 202
        assert response.json()["id"] == task_id
        assert (
            response.json()["status"],
            response.json()["stage"],
            response.json()["progress"],
        ) == (
            "queued",
            "queued",
            0,
        )
        assert response.json()["findings"] == []
        assert executor.submitted == [task_id]
    finally:
        client.__exit__(None, None, None)


def test_retry_captures_one_document_review_snapshot_for_the_attempt(
    tmp_path, monkeypatch
):
    client, executor = _client(tmp_path)
    try:
        with client.app.state.session_factory() as session:
            task = ReviewTask(
                document_filename="proposal.txt",
                document_type="technical_proposal",
                file_path=str(tmp_path / "proposal.txt"),
                status="failed",
                stage="reviewing",
                progress=40,
                error_summary="provider unavailable",
            )
            session.add(task)
            session.commit()
            task_id = task.id

        router = client.app.state.feature_provider_router
        original_snapshot = router.snapshot
        features = []

        def recording_snapshot(feature):
            features.append(feature)
            return original_snapshot(feature)

        monkeypatch.setattr(router, "snapshot", recording_snapshot)
        response = client.post(f"/api/reviews/{task_id}/retry")

        assert response.status_code == 202
        assert features == ["document_review"]
        assert len(executor.submitted) == 1
        submitted_task_id = executor.submitted[0]
        submitted_snapshot = executor.snapshots[0]
        assert submitted_task_id == task_id
        assert submitted_snapshot.feature == "document_review"
        with client.app.state.session_factory() as session:
            audit = session.scalar(
                select(AuditEvent).where(
                    AuditEvent.event_type == "review.retried",
                    AuditEvent.reference_id == task_id,
                )
            )
        assert audit.mode == submitted_snapshot.mode
    finally:
        client.__exit__(None, None, None)


def test_retry_rejects_active_task_and_cancel_marks_it_cancelled(tmp_path):
    client, executor = _client(tmp_path)
    try:
        with client.app.state.session_factory() as session:
            task = ReviewTask(
                document_filename="proposal.txt",
                document_type="technical_proposal",
                file_path=str(tmp_path / "proposal.txt"),
                status="reviewing",
                stage="reviewing",
                progress=60,
            )
            session.add(task)
            session.commit()
            task_id = task.id

        retry = client.post(f"/api/reviews/{task_id}/retry")
        assert retry.status_code == 409
        assert retry.json() == {"detail": "仅失败、已取消或已中断的审核任务可以重试"}
        cancelled = client.post(f"/api/reviews/{task_id}/cancel")
        assert cancelled.status_code == 200
        assert cancelled.json()["status"] == "cancelled"
        assert executor.cancelled == [task_id]
        repeated = client.post(f"/api/reviews/{task_id}/cancel")
        assert repeated.status_code == 409
        assert repeated.json() == {"detail": "审核任务当前不可取消"}
    finally:
        client.__exit__(None, None, None)


def test_export_csv_has_utf8_bom_chinese_headers_and_rule_source(tmp_path):
    client, _ = _client(tmp_path)
    try:
        with client.app.state.session_factory() as session:
            rule = session.scalar(select(ReviewRule).limit(1))
            task = ReviewTask(
                document_filename="proposal.txt",
                document_type=rule.document_type,
                file_path=str(tmp_path / "proposal.txt"),
                status="completed",
                stage="completed",
                progress=100,
            )
            session.add(task)
            session.flush()
            session.add(
                ReviewFinding(
                    review_task_id=task.id,
                    review_rule_id=rule.id,
                    risk="high",
                    description='=HYPERLINK("https://invalid.example")',
                    location="第一章",
                    excerpt="项目范围",
                    recommendation="补充可量化指标",
                )
            )
            session.commit()
            task_id = task.id
            source = rule.source

        response = client.get(f"/api/reviews/{task_id}/export.csv")

        assert response.status_code == 200
        assert response.content.startswith(b"\xef\xbb\xbf")
        text = response.content.decode("utf-8-sig")
        assert (
            text.splitlines()[0]
            == "风险等级,问题描述,原文位置,原文摘录,规则出处,修改建议,人工复核状态"
        )
        assert source in text
        assert "'=HYPERLINK" in text
        rows = list(csv.reader(StringIO(text)))
        assert rows[1][0] == "高"
        assert rows[1][6] == "待复核"
    finally:
        client.__exit__(None, None, None)


def test_startup_marks_unfinished_tasks_interrupted(tmp_path):
    settings = Settings(runtime_dir=tmp_path)
    first_app = create_app(settings)
    with TestClient(first_app):
        with first_app.state.session_factory() as session:
            active = ReviewTask(
                document_filename="proposal.txt",
                document_type="technical_proposal",
                file_path=str(tmp_path / "proposal.txt"),
                status="reviewing",
                stage="reviewing",
                progress=60,
            )
            completed = ReviewTask(
                document_filename="done.txt",
                document_type="technical_proposal",
                file_path=str(tmp_path / "done.txt"),
                status="completed",
                stage="completed",
                progress=100,
            )
            session.add_all([active, completed])
            session.commit()
            active_id, completed_id = active.id, completed.id

    second_app = create_app(settings)
    with TestClient(second_app):
        with second_app.state.session_factory() as session:
            assert session.get(ReviewTask, active_id).status == "interrupted"
            assert (
                session.get(ReviewTask, active_id).error_summary
                == "服务重启导致本次审核中断，请重试。"
            )
            assert session.get(ReviewTask, completed_id).status == "completed"
            interrupted_audits = list(
                session.scalars(
                    select(AuditEvent).where(
                        AuditEvent.event_type == "review.interrupted",
                        AuditEvent.reference_id == active_id,
                    )
                )
            )
            assert len(interrupted_audits) == 1


def test_design_document_uses_seeded_rules_and_produces_findings(tmp_path):
    app = create_app(Settings(runtime_dir=tmp_path))
    with TestClient(app) as client:
        response = client.post(
            "/api/reviews",
            data={"document_type": "design_document"},
            files={
                "file": (
                    "design.txt",
                    "一、设计概述\n本文说明总体思路。".encode(),
                    "text/plain",
                )
            },
        )

        assert response.status_code == 202
        task_id = response.json()["id"]
        deadline = monotonic() + 2
        task = response.json()
        while task["status"] not in {"completed", "failed"} and monotonic() < deadline:
            sleep(0.01)
            task = client.get(f"/api/reviews/{task_id}").json()

        assert task["status"] == "completed"
        assert task["findings"]
        with app.state.session_factory() as session:
            assert (
                session.scalar(
                    select(ReviewRule)
                    .where(ReviewRule.document_type == "design_document")
                    .limit(1)
                )
                is not None
            )


def test_tender_document_uses_seeded_rules_and_produces_findings(tmp_path):
    app = create_app(Settings(runtime_dir=tmp_path))
    with TestClient(app) as client:
        response = client.post(
            "/api/reviews",
            data={"document_type": "tender_document"},
            files={
                "file": (
                    "tender.txt",
                    "一、招标概述\n本文说明项目背景。".encode(),
                    "text/plain",
                )
            },
        )

        assert response.status_code == 202
        task = _wait_for_terminal(client, response.json())
        assert task["status"] == "completed"
        assert task["findings"]


def test_offline_review_recalls_global_rules_once_without_irrelevant_duplicates(tmp_path):
    sample = (
        Path(__file__).resolve().parents[1]
        / "app"
        / "data"
        / "review_samples"
        / "technical-proposal.txt"
    )
    app = create_app(Settings(runtime_dir=tmp_path))
    with TestClient(app) as client:
        response = client.post(
            "/api/reviews",
            data={"document_type": "technical_proposal"},
            files={"file": (sample.name, sample.read_bytes(), "text/plain")},
        )
        task = _wait_for_terminal(client, response.json())
        with app.state.session_factory() as session:
            rows = session.execute(
                select(ReviewRule.check_item, ReviewFinding.location)
                .join(ReviewFinding, ReviewFinding.review_rule_id == ReviewRule.id)
                .where(ReviewFinding.review_task_id == task["id"])
            ).all()
            audit_types = {
                event.event_type
                for event in session.scalars(
                    select(AuditEvent).where(AuditEvent.reference_id == task["id"])
                )
            }

    assert task["status"] == "completed"
    by_rule = {}
    for check_item, location in rows:
        by_rule.setdefault(check_item, []).append(location)
    assert set(by_rule) == {"验收指标", "运维交接", "培训计划", "风险管理", "变更控制"}
    assert all(len(locations) == 1 for locations in by_rule.values())
    assert all(locations == ["全文"] for locations in by_rule.values())
    assert audit_types >= {
        "review.submitted",
        "review.stage.parsing",
        "review.stage.matching_rules",
        "review.stage.reviewing",
        "review.stage.summarizing",
        "review.completed",
    }


def test_review_submission_retry_and_cancel_audits_are_fixed_and_redacted(tmp_path):
    secret_document = "不得写入审计的文档正文"
    client, _ = _client(tmp_path)
    try:
        created = client.post(
            "/api/reviews",
            data={"document_type": "technical_proposal"},
            files={"file": ("proposal.txt", secret_document.encode(), "text/plain")},
        ).json()
        with client.app.state.session_factory() as session:
            task = session.get(ReviewTask, created["id"])
            task.status = "failed"
            session.commit()
        retried = client.post(f"/api/reviews/{created['id']}/retry").json()
        cancelled = client.post(f"/api/reviews/{created['id']}/cancel").json()
        with client.app.state.session_factory() as session:
            events = list(
                session.scalars(
                    select(AuditEvent).where(AuditEvent.reference_id == created["id"])
                )
            )

        assert retried["status"] == "queued"
        assert cancelled["status"] == "cancelled"
        assert {event.event_type for event in events} >= {
            "review.submitted", "review.retried", "review.cancelled"
        }
        assert secret_document not in " ".join(event.request_summary for event in events)
        assert all(event.mode == "mock" for event in events)
    finally:
        client.__exit__(None, None, None)


def test_create_compensates_closed_executor_submission(tmp_path):
    client, _ = _client(tmp_path, raise_server_exceptions=False)
    try:
        _install_closed_executor(client)

        response = client.post(
            "/api/reviews",
            data={"document_type": "technical_proposal"},
            files={"file": ("proposal.txt", "一、范围\n正文".encode(), "text/plain")},
        )

        assert response.status_code == 202
        assert response.json()["status"] == "failed"
        assert response.json()["error_summary"] == "审核任务提交失败，请稍后重试。"
    finally:
        client.__exit__(None, None, None)


def test_retry_compensates_closed_executor_submission(tmp_path):
    client, _ = _client(tmp_path, raise_server_exceptions=False)
    try:
        with client.app.state.session_factory() as session:
            task = ReviewTask(
                document_filename="proposal.txt",
                document_type="technical_proposal",
                file_path=str(tmp_path / "proposal.txt"),
                status="failed",
                stage="queued",
                progress=0,
                error_summary="旧错误",
            )
            session.add(task)
            session.commit()
            task_id = task.id
        _install_closed_executor(client)

        response = client.post(f"/api/reviews/{task_id}/retry")

        assert response.status_code == 202
        assert response.json()["status"] == "failed"
        assert response.json()["error_summary"] == "审核任务提交失败，请稍后重试。"
    finally:
        client.__exit__(None, None, None)


def test_create_returns_chinese_500_when_submission_compensation_also_fails(
    tmp_path, monkeypatch, caplog
):
    client, _ = _client(tmp_path, raise_server_exceptions=False)
    original_execute = Session.execute

    def fail_review_task_update(session, statement, *args, **kwargs):
        if isinstance(statement, Update) and statement.table.name == "review_tasks":
            raise RuntimeError("database unavailable")
        return original_execute(session, statement, *args, **kwargs)

    try:
        _install_closed_executor(client)
        monkeypatch.setattr(Session, "execute", fail_review_task_update)

        response = client.post(
            "/api/reviews",
            data={"document_type": "technical_proposal"},
            files={"file": ("proposal.txt", "一、范围\n正文".encode(), "text/plain")},
        )

        assert response.status_code == 500
        assert response.json() == {
            "detail": "审核任务提交失败，且无法保存失败状态，请稍后重试"
        }
        assert "失败补偿持久化失败" in caplog.text
    finally:
        client.__exit__(None, None, None)


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("\n=1+1", "'\n=1+1"),
        (" \t\r\n+1", "' \t\r\n+1"),
        ("\x01@SUM(A1:A2)", "'\x01@SUM(A1:A2)"),
        ("  -2", "'  -2"),
        ("普通文本", "普通文本"),
        (" 普通文本", " 普通文本"),
    ],
)
def test_spreadsheet_safe_scans_leading_whitespace_and_controls(value, expected):
    assert _spreadsheet_safe(value) == expected


@pytest.mark.parametrize(
    ("value", "expected"),
    [("high", "高"), ("medium", "中"), ("low", "低")],
)
def test_csv_risk_labels_are_chinese(value, expected):
    assert _risk_label(value) == expected


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("pending", "待复核"),
        ("confirmed", "已确认"),
        ("dismissed", "已忽略"),
        ("approved", "已通过"),
        ("rejected", "已驳回"),
        ("resolved", "已处理"),
    ],
)
def test_csv_human_review_labels_are_chinese(value, expected):
    assert _human_review_label(value) == expected


def _install_closed_executor(client):
    executor = ReviewExecutor(
        client.app.state.session_factory,
        client.app.state.inference_coordinator,
    )
    executor.shutdown()
    client.app.state.review_executor = executor


def _wait_for_terminal(client, task):
    deadline = monotonic() + 2
    while task["status"] not in {"completed", "failed"} and monotonic() < deadline:
        sleep(0.01)
        task = client.get(f"/api/reviews/{task['id']}").json()
    return task
