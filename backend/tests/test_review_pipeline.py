from pathlib import Path
from concurrent.futures import Future
from threading import Event, Thread
from time import monotonic, sleep

import pytest
from sqlalchemy import create_engine, delete, select
from sqlalchemy.orm import sessionmaker

from app.models import (
    AuditEvent,
    Base,
    ModelRequestMetric,
    ReviewFinding,
    ReviewRule,
    ReviewTask,
)
from app.providers.base import ProviderUnavailable, ProviderUsage
from app.providers.feature_router import FeatureProviderSnapshot
from app.schemas import ReviewFindingInput, ReviewRuleInput
from app.services.inference import InferenceCoordinator
from app.services.review_pipeline import (
    InvalidTaskTransition,
    ReviewPipeline,
    ReviewTaskState,
    merge_findings,
    review_with_one_repair,
)
from app.services.review_executor import ReviewExecutor


def test_invalid_transition_is_rejected():
    task = ReviewTaskState(status="queued", progress=0)

    with pytest.raises(InvalidTaskTransition):
        task.transition("completed", progress=100)


def test_state_machine_accepts_the_persisted_stage_sequence():
    task = ReviewTaskState(status="queued", progress=0)

    for status, progress in [
        ("parsing", 10),
        ("matching_rules", 25),
        ("reviewing", 40),
        ("summarizing", 90),
        ("completed", 100),
    ]:
        task.transition(status, progress)

    assert task.status == "completed"
    assert task.progress == 100


def test_queued_task_can_be_compensated_to_failed():
    task = ReviewTaskState(status="queued", progress=0)

    task.transition("failed", progress=0)

    assert task.status == "failed"


def test_state_machine_rejects_decreasing_progress():
    task = ReviewTaskState(status="reviewing", progress=60)

    with pytest.raises(InvalidTaskTransition, match="progress"):
        task.transition("summarizing", progress=50)


def test_duplicate_findings_are_merged_by_rule_and_normalized_location():
    findings = [
        ReviewFindingInput(
            rule_id="rule-1",
            risk="medium",
            description="first",
            location=" 第一章 ",
            excerpt="text",
            recommendation="fix",
        ),
        ReviewFindingInput(
            rule_id="rule-1",
            risk="high",
            description="duplicate",
            location="第 一 章",
            excerpt="text",
            recommendation="fix",
        ),
        ReviewFindingInput(
            rule_id="rule-2",
            risk="low",
            description="different rule",
            location="第一章",
            excerpt="text",
            recommendation="fix",
        ),
    ]

    merged = merge_findings(findings)

    assert [(finding.rule_id, finding.risk) for finding in merged] == [
        ("rule-1", "high"),
        ("rule-2", "low"),
    ]


class _RepairingProvider:
    def __init__(self):
        self.sections = []

    def review_section(self, document_type, section, rules):
        self.sections.append(section)
        if len(self.sections) == 1:
            raise ProviderUnavailable("Provider returned invalid structured JSON")
        return [
            ReviewFindingInput(
                rule_id=rules[0].id,
                risk="high",
                description="missing acceptance metric",
                location="一、项目范围",
                excerpt="section",
                recommendation="add a measurable metric",
            )
        ]


def test_structured_output_gets_exactly_one_repair_attempt():
    provider = _RepairingProvider()
    rules = [
        ReviewRuleInput(
            id="rule-1",
            document_type="technical_proposal",
            check_item="acceptance metric",
            criterion="must be measurable",
            source="demo rule",
            default_risk="high",
        )
    ]

    findings = review_with_one_repair(provider, "technical_proposal", "section", rules)

    assert len(provider.sections) == 2
    assert "修复" in provider.sections[1]
    assert findings[0].rule_id == "rule-1"


class _FabricatingThenGroundedProvider:
    def __init__(self):
        self.calls = 0

    def review_section(self, document_type, section, rules):
        self.calls += 1
        return [
            ReviewFindingInput(
                rule_id=rules[0].id,
                risk="high",
                description="missing acceptance metric",
                location="current section",
                excerpt="fabricated quote" if self.calls == 1 else "source sentence",
                recommendation="add a measurable metric",
            )
        ]


def test_fabricated_excerpt_gets_exactly_one_repair_attempt():
    provider = _FabricatingThenGroundedProvider()
    rules = [
        ReviewRuleInput(
            id="rule-1",
            document_type="technical_proposal",
            check_item="acceptance metric",
            criterion="must be measurable",
            source="demo rule",
            default_risk="high",
        )
    ]

    findings = review_with_one_repair(
        provider, "technical_proposal", "source sentence", rules
    )

    assert provider.calls == 2
    assert findings[0].excerpt == "source sentence"


class _AlwaysMalformedProvider:
    def __init__(self):
        self.calls = 0

    def review_section(self, document_type, section, rules):
        self.calls += 1
        raise ProviderUnavailable("Provider returned invalid structured JSON")


def test_second_structured_output_failure_is_raised():
    provider = _AlwaysMalformedProvider()

    with pytest.raises(ProviderUnavailable, match="structured JSON"):
        review_with_one_repair(provider, "technical_proposal", "section", [])

    assert provider.calls == 2


class _WhitespaceExcerptProvider:
    def __init__(self):
        self.calls = 0

    def review_section(self, document_type, section, rules):
        self.calls += 1
        return [
            ReviewFindingInput(
                rule_id=rules[0].id,
                risk="high",
                description="missing item",
                location="current section",
                excerpt=" ",
                recommendation="add item",
            )
        ]


def test_whitespace_excerpt_fails_after_one_repair_attempt():
    provider = _WhitespaceExcerptProvider()
    rules = [
        ReviewRuleInput(
            id="rule-1",
            document_type="technical_proposal",
            check_item="scope",
            criterion="required",
            source="demo rule",
            default_risk="high",
        )
    ]

    with pytest.raises(ValueError, match="excerpt"):
        review_with_one_repair(provider, "technical_proposal", "source sentence", rules)

    assert provider.calls == 2


class _DirectCoordinator:
    def execute(
        self,
        feature,
        operation,
        snapshot,
        callback,
    ):
        assert feature == snapshot.feature == "document_review"
        assert operation == "document_review"
        return callback()


def _review_snapshot(provider, mode="mock"):
    return FeatureProviderSnapshot(
        feature="document_review",
        mode=mode,
        model=(
            "qwen3:1.7b-q4_K_M"
            if mode == "local"
            else None
        ),
        provider=provider,
    )


class _CompletingProvider:
    def review_section(self, document_type, section, rules):
        return [
            ReviewFindingInput(
                rule_id=rules[0].id,
                risk=rules[0].default_risk,
                description="missing item",
                location="当前章节",
                excerpt=section,
                recommendation="add item",
            )
        ]


class _FailingProvider:
    @property
    def last_usage(self):
        return ProviderUsage(input_tokens=3, output_tokens=0)

    def reset_usage(self):
        return None

    def review_section(self, document_type, section, rules):
        raise RuntimeError("provider attempt failed")


class _MeasuredCompletingProvider(_CompletingProvider):
    @property
    def last_usage(self):
        return ProviderUsage(input_tokens=7, output_tokens=5)

    def reset_usage(self):
        return None


def test_retry_attempt_keeps_one_snapshot_for_audit_and_metrics(tmp_path):
    database_url = f"sqlite:///{tmp_path / 'retry-snapshot.db'}"
    engine = create_engine(
        database_url,
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine)
    coordinator = InferenceCoordinator(
        factory,
        telemetry_database_url=database_url,
    )
    document_path = Path(tmp_path) / "proposal.txt"
    document_path.write_text(
        "一、项目范围\n范围待补充。",
        encoding="utf-8",
    )
    with factory() as session:
        session.add(
            ReviewRule(
                id="rule-1",
                document_type="technical_proposal",
                check_item="acceptance metric",
                criterion="must be measurable",
                source="demo rule",
                default_risk="high",
                status="active",
            )
        )
        task = ReviewTask(
            document_filename="proposal.txt",
            document_type="technical_proposal",
            file_path=str(document_path),
        )
        session.add(task)
        session.commit()
        task_id = task.id

    pipeline = ReviewPipeline(factory, coordinator)
    first_snapshot = FeatureProviderSnapshot(
        feature="document_review",
        mode="mock",
        model=None,
        provider=_FailingProvider(),
    )
    second_snapshot = FeatureProviderSnapshot(
        feature="document_review",
        mode="local",
        model="qwen3:1.7b-q4_K_M",
        provider=_MeasuredCompletingProvider(),
    )
    try:
        pipeline.run(task_id, first_snapshot)
        with factory() as session:
            first_audits = list(
                session.scalars(
                    select(AuditEvent).where(
                        AuditEvent.reference_id == task_id
                    )
                )
            )
            task = session.get(ReviewTask, task_id)
            assert task.status == "failed"
            assert {audit.mode for audit in first_audits} == {"mock"}
            first_audit_ids = [audit.id for audit in first_audits]
            task.status = "queued"
            task.stage = "queued"
            task.progress = 0
            task.error_summary = None
            task.started_at = None
            task.completed_at = None
            session.commit()

        pipeline.run(task_id, second_snapshot)
        deadline = monotonic() + 2
        metrics = []
        while len(metrics) < 2 and monotonic() < deadline:
            with factory() as session:
                metrics = list(
                    session.scalars(
                        select(ModelRequestMetric).where(
                            ModelRequestMetric.feature
                            == "document_review"
                        )
                    )
                )
            if len(metrics) < 2:
                sleep(0.01)
        with factory() as session:
            second_audits = list(
                session.scalars(
                    select(AuditEvent).where(
                        AuditEvent.reference_id == task_id,
                        AuditEvent.id.not_in(
                            first_audit_ids
                        ),
                    )
                )
            )
            task = session.get(ReviewTask, task_id)

        assert task.status == "completed"
        assert {audit.mode for audit in second_audits} == {"local"}
        assert {
            (metric.mode, metric.result_status)
            for metric in metrics
        } == {
            ("mock", "error"),
            ("local", "success"),
        }
    finally:
        assert coordinator.shutdown(timeout_seconds=1)
        engine.dispose()


def test_pipeline_completes_and_persists_findings(tmp_path):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'review.db'}", connect_args={"check_same_thread": False}
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine)
    document_path = Path(tmp_path) / "proposal.txt"
    document_path.write_text("一、项目范围\n范围待补充。", encoding="utf-8")
    with factory() as session:
        rule = ReviewRule(
            id="rule-1",
            document_type="technical_proposal",
            check_item="acceptance metric",
            criterion="must be measurable",
            source="demo rule",
            default_risk="high",
            status="active",
        )
        task = ReviewTask(
            document_filename="proposal.txt",
            document_type="technical_proposal",
            file_path=str(document_path),
        )
        session.add_all([rule, task])
        session.commit()
        task_id = task.id

    ReviewPipeline(factory, _DirectCoordinator()).run(
        task_id,
        _review_snapshot(_CompletingProvider()),
    )

    with factory() as session:
        task = session.get(ReviewTask, task_id)
        findings = list(
            session.scalars(
                select(ReviewFinding).where(ReviewFinding.review_task_id == task_id)
            )
        )
        assert (task.status, task.stage, task.progress, task.error_summary) == (
            "completed",
            "completed",
            100,
            None,
        )
        assert len(findings) == 1
        assert findings[0].review_rule_id == "rule-1"
        assert findings[0].location == "全文"
        assert task.started_at is not None
        assert task.completed_at is not None
    engine.dispose()


def test_missing_section_local_topic_becomes_one_document_completeness_finding(tmp_path):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'local-recall.db'}",
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine)
    document_path = Path(tmp_path) / "design.txt"
    document_path.write_text("一、实施计划\n项目按四周实施。", encoding="utf-8")
    with factory() as session:
        session.add(
            ReviewRule(
                id="security-design-rule",
                document_type="design_document",
                check_item="安全设计",
                criterion="说明访问控制与密钥保护。",
                source="演示安全基线",
                default_risk="high",
            )
        )
        task = ReviewTask(
            document_filename="design.txt",
            document_type="design_document",
            file_path=str(document_path),
        )
        session.add(task)
        session.commit()
        task_id = task.id

    ReviewPipeline(factory, _DirectCoordinator()).run(
        task_id,
        _review_snapshot(_CompletingProvider()),
    )

    with factory() as session:
        findings = list(
            session.scalars(
                select(ReviewFinding).where(ReviewFinding.review_task_id == task_id)
            )
        )
    assert [(finding.review_rule_id, finding.location) for finding in findings] == [
        ("security-design-rule", "全文")
    ]
    engine.dispose()


def test_pipeline_marks_second_malformed_response_failed(tmp_path):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'review.db'}", connect_args={"check_same_thread": False}
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine)
    document_path = Path(tmp_path) / "proposal.txt"
    document_path.write_text("一、项目范围\n范围待补充。", encoding="utf-8")
    with factory() as session:
        session.add(
            ReviewRule(
                id="rule-1",
                document_type="technical_proposal",
                check_item="scope",
                criterion="required",
                source="demo rule",
                default_risk="high",
            )
        )
        task = ReviewTask(
            document_filename="proposal.txt",
            document_type="technical_proposal",
            file_path=str(document_path),
        )
        session.add(task)
        session.commit()
        task_id = task.id

    provider = _AlwaysMalformedProvider()
    ReviewPipeline(factory, _DirectCoordinator()).run(
        task_id,
        _review_snapshot(provider),
    )

    with factory() as session:
        task = session.get(ReviewTask, task_id)
        assert task.status == "failed"
        assert task.stage == "reviewing"
        assert task.progress < 100
        assert task.error_summary == "模型审核失败：模型服务不可用或返回格式无效。"
    assert provider.calls == 2
    engine.dispose()


def test_initial_transition_failure_compensates_queued_task(tmp_path, monkeypatch):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'review.db'}", connect_args={"check_same_thread": False}
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine)
    with factory() as session:
        task = ReviewTask(
            document_filename="proposal.txt",
            document_type="technical_proposal",
            file_path=str(tmp_path / "proposal.txt"),
        )
        session.add(task)
        session.commit()
        task_id = task.id
    pipeline = ReviewPipeline(factory, _DirectCoordinator())
    snapshot = _review_snapshot(_CompletingProvider())
    monkeypatch.setattr(
        pipeline,
        "_transition",
        lambda *args: (_ for _ in ()).throw(RuntimeError("boom")),
    )

    pipeline.run(task_id, snapshot)

    with factory() as session:
        task = session.get(ReviewTask, task_id)
        assert task.status == "failed"
        assert task.progress == 0
        assert task.error_summary == "文档审核失败，请稍后重试。"
    engine.dispose()


class _ImmediatePool:
    def submit(self, function, *args):
        future = Future()
        function(*args)
        future.set_result(None)
        return future

    def shutdown(self, **kwargs):
        pass


def test_executor_does_not_deadlock_when_future_finishes_during_submit(monkeypatch):
    monkeypatch.setattr(
        "app.services.review_executor.ThreadPoolExecutor", lambda **_: _ImmediatePool()
    )
    executor = ReviewExecutor(lambda: None, _DirectCoordinator())
    executor._pipeline.run = lambda task_id, snapshot: None
    snapshot = _review_snapshot(_CompletingProvider())
    submitting = Thread(
        target=executor.submit,
        args=("task-1", snapshot),
        daemon=True,
    )

    submitting.start()
    submitting.join(timeout=0.5)

    assert not submitting.is_alive()


def test_executor_is_bounded_to_two_workers():
    assert ReviewExecutor.max_workers == 2


def test_executor_queues_retry_behind_finishing_attempt():
    executor = ReviewExecutor(lambda: None, _DirectCoordinator())
    first_started = Event()
    release_first = Event()
    second_finished = Event()
    calls = []

    def controlled_run(task_id, snapshot):
        assert snapshot.feature == "document_review"
        calls.append(task_id)
        if len(calls) == 1:
            first_started.set()
            release_first.wait(timeout=2)
        else:
            second_finished.set()

    executor._pipeline.run = controlled_run
    first_snapshot = _review_snapshot(_CompletingProvider())
    retry_snapshot = _review_snapshot(
        _CompletingProvider(),
        mode="local",
    )
    try:
        executor.submit("task-1", first_snapshot)
        assert first_started.wait(timeout=1)

        executor.submit("task-1", retry_snapshot)
        release_first.set()

        assert second_finished.wait(timeout=1)
        assert calls == ["task-1", "task-1"]
    finally:
        release_first.set()
        executor.shutdown()


def test_stale_cancelled_attempt_cannot_persist_findings_after_retry(
    tmp_path, monkeypatch
):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'review.db'}", connect_args={"check_same_thread": False}
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine)
    document_path = Path(tmp_path) / "proposal.txt"
    document_path.write_text("一、项目范围\n范围待补充。", encoding="utf-8")
    with factory() as session:
        session.add(
            ReviewRule(
                id="rule-1",
                document_type="technical_proposal",
                check_item="scope",
                criterion="required",
                source="demo rule",
                default_risk="high",
            )
        )
        task = ReviewTask(
            document_filename="proposal.txt",
            document_type="technical_proposal",
            file_path=str(document_path),
        )
        session.add(task)
        session.commit()
        task_id = task.id

    summarizing = Event()
    release_summary = Event()
    import app.services.review_pipeline as review_pipeline_module

    original_merge = review_pipeline_module.merge_findings

    def blocking_merge(findings):
        summarizing.set()
        release_summary.wait(timeout=2)
        return original_merge(findings)

    monkeypatch.setattr(review_pipeline_module, "merge_findings", blocking_merge)
    pipeline = ReviewPipeline(factory, _DirectCoordinator())
    worker = Thread(
        target=pipeline.run,
        args=(
            task_id,
            _review_snapshot(_CompletingProvider()),
        ),
    )
    worker.start()
    assert summarizing.wait(timeout=1)

    with factory() as session:
        task = session.get(ReviewTask, task_id)
        task.status = "cancelled"
        session.commit()
    with factory() as session:
        task = session.get(ReviewTask, task_id)
        session.execute(
            delete(ReviewFinding).where(ReviewFinding.review_task_id == task_id)
        )
        task.status = "queued"
        task.stage = "queued"
        task.progress = 0
        session.commit()

    release_summary.set()
    worker.join(timeout=1)

    with factory() as session:
        task = session.get(ReviewTask, task_id)
        findings = list(
            session.scalars(
                select(ReviewFinding).where(ReviewFinding.review_task_id == task_id)
            )
        )
        assert task.status == "queued"
        assert findings == []
    engine.dispose()
