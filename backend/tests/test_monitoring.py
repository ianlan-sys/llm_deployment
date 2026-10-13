import json
from datetime import datetime, timedelta, timezone
from threading import Event, Thread
from threading import enumerate as enumerate_threads
from time import monotonic, sleep

import pytest
from sqlalchemy import create_engine, select, text
from sqlalchemy.orm import sessionmaker

from app.models import ModelRequestMetric, ResourceSample
from app.services.monitoring import MonitoringService, ResourceSampler, percentile_95


NOW = datetime(2026, 7, 24, 4, 7, tzinfo=timezone.utc)


def _metric(
    request_id: str,
    *,
    created_at: datetime,
    feature: str = "knowledge",
    mode: str = "local",
    result_status: str = "success",
    elapsed_ms: int = 100,
    ttft_ms: int | None = 20,
    queue_wait_ms: int = 5,
    input_tokens: int | None = 10,
    output_tokens: int | None = 4,
) -> ModelRequestMetric:
    return ModelRequestMetric(
        feature=feature,
        mode=mode,
        model="qwen3:1.7b-q4_K_M",
        request_id=request_id,
        operation="knowledge_answer",
        queue_wait_ms=queue_wait_ms,
        ttft_ms=ttft_ms,
        elapsed_ms=elapsed_ms,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        result_status=result_status,
        created_at=created_at,
    )


def _resource(
    *,
    created_at: datetime,
    queued_count: int = 0,
) -> ResourceSample:
    return ResourceSample(
        cpu_percent=12.5,
        memory_percent=34.5,
        disk_percent=56.5,
        ollama_available=True,
        model_loaded=True,
        running_count=1,
        queued_count=queued_count,
        created_at=created_at,
    )


def _factory(tmp_path):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'monitoring.db'}",
        connect_args={"check_same_thread": False},
    )
    from app.models import Base

    Base.metadata.create_all(engine)
    return engine, sessionmaker(bind=engine)


def _wait_until(predicate, timeout: float = 1.0) -> None:
    deadline = monotonic() + timeout
    while monotonic() < deadline:
        if predicate():
            return
        sleep(0.005)
    raise AssertionError("condition was not met before timeout")


def test_percentile_95_uses_nearest_rank():
    assert percentile_95([]) == 0
    assert percentile_95([500, 100, 400, 200, 300]) == 500
    assert percentile_95(list(range(1, 101))) == 95


def test_history_keeps_24_hours_and_groups_by_bucket_feature_mode_and_result(
    tmp_path,
):
    engine, factory = _factory(tmp_path)
    try:
        with factory() as session:
            session.add_all(
                [
                    _metric(
                        "one",
                        created_at=NOW.replace(minute=1),
                        elapsed_ms=100,
                        ttft_ms=10,
                        queue_wait_ms=2,
                        input_tokens=5,
                        output_tokens=3,
                    ),
                    _metric(
                        "two",
                        created_at=NOW.replace(minute=4),
                        elapsed_ms=200,
                        ttft_ms=30,
                        queue_wait_ms=8,
                        input_tokens=7,
                        output_tokens=4,
                    ),
                    _metric(
                        "three",
                        created_at=NOW.replace(minute=6),
                        feature="call_assistant",
                        mode="mock",
                        result_status="timeout",
                        elapsed_ms=300,
                        ttft_ms=None,
                        queue_wait_ms=12,
                        input_tokens=None,
                        output_tokens=None,
                    ),
                    _metric(
                        "expired",
                        created_at=NOW - timedelta(hours=25),
                        elapsed_ms=9999,
                    ),
                    _resource(
                        created_at=NOW - timedelta(minutes=1),
                        queued_count=3,
                    ),
                    _resource(created_at=NOW - timedelta(hours=25)),
                ]
            )
            session.commit()

        history = MonitoringService(factory, clock=lambda: NOW).history(hours=24)

        assert history.total_requests == 3
        assert history.average_elapsed_ms == 200
        assert history.p95_elapsed_ms == 300
        assert history.average_ttft_ms == 20
        assert history.average_queue_wait_ms == pytest.approx(22 / 3)
        assert history.max_queue_depth == 3
        assert history.input_tokens == 12
        assert history.output_tokens == 7
        assert len(history.requests) == 2
        first = history.requests[0]
        assert first.created_at == NOW.replace(minute=0, second=0, microsecond=0)
        assert first.feature == "knowledge"
        assert first.mode == "local"
        assert first.result_status == "success"
        assert first.request_count == 2
        assert first.average_elapsed_ms == 150
        assert first.p95_elapsed_ms == 200
        assert first.average_ttft_ms == 20
        assert first.average_queue_wait_ms == 5
        assert first.input_tokens == 12
        assert first.output_tokens == 7
        assert len(history.resources) == 1
    finally:
        engine.dispose()


def test_history_rejects_windows_outside_retention(tmp_path):
    engine, factory = _factory(tmp_path)
    try:
        service = MonitoringService(factory, clock=lambda: NOW)
        with pytest.raises(ValueError, match="between 1 and 24"):
            service.history(hours=0)
        with pytest.raises(ValueError, match="between 1 and 24"):
            service.history(hours=25)
    finally:
        engine.dispose()


def test_history_includes_exact_cutoff_and_excludes_future_rows(tmp_path):
    engine, factory = _factory(tmp_path)
    cutoff = NOW - timedelta(hours=24)
    clock_calls = 0

    def clock():
        nonlocal clock_calls
        clock_calls += 1
        if clock_calls > 1:
            raise AssertionError("history clock called more than once")
        return NOW

    try:
        with factory() as session:
            session.add_all(
                [
                    _metric("at-cutoff", created_at=cutoff),
                    _metric(
                        "before-cutoff",
                        created_at=cutoff - timedelta(microseconds=1),
                    ),
                    _metric(
                        "future",
                        created_at=NOW + timedelta(microseconds=1),
                    ),
                    _resource(created_at=cutoff),
                    _resource(
                        created_at=cutoff - timedelta(microseconds=1),
                    ),
                    _resource(
                        created_at=NOW + timedelta(microseconds=1),
                    ),
                ]
            )
            session.commit()

        history = MonitoringService(factory, clock=clock).history(hours=24)

        assert clock_calls == 1
        assert history.total_requests == 1
        assert len(history.resources) == 1
        assert history.resources[0].created_at == cutoff
    finally:
        engine.dispose()


def test_history_only_parses_database_bounded_candidates(
    tmp_path, monkeypatch
):
    import app.services.monitoring as monitoring_module

    engine, factory = _factory(tmp_path)
    parsed_values = []
    real_safe_datetime = monitoring_module._safe_datetime

    def counted_safe_datetime(value):
        parsed_values.append(value)
        return real_safe_datetime(value)

    monkeypatch.setattr(
        monitoring_module,
        "_safe_datetime",
        counted_safe_datetime,
    )
    try:
        with factory() as session:
            session.add(_metric("recent", created_at=NOW))
            session.add(_resource(created_at=NOW))
            julian_now = session.scalar(
                text("SELECT julianday(:now)"),
                {"now": NOW},
            )
            session.execute(
                text(
                    "INSERT INTO model_request_metrics "
                    "(id, feature, mode, model, request_id, operation, "
                    "queue_wait_ms, elapsed_ms, result_status, created_at) "
                    "VALUES "
                    "('numeric-time-metric', 'knowledge', 'local', "
                    "'local', 'numeric-time', 'knowledge_answer', "
                    "0, 1, 'error', :created_at)"
                ),
                {"created_at": julian_now},
            )
            session.execute(
                text(
                    "INSERT INTO resource_samples "
                    "(id, cpu_percent, memory_percent, disk_percent, "
                    "ollama_available, model_loaded, running_count, "
                    "queued_count, created_at) "
                    "VALUES "
                    "('numeric-time-resource', 10, 20, 30, "
                    "0, 0, 0, 0, :created_at)"
                ),
                {"created_at": julian_now},
            )
            for index in range(20):
                expired_at = NOW - timedelta(hours=25, minutes=index)
                session.add(
                    _metric(f"expired-{index}", created_at=expired_at)
                )
                session.add(_resource(created_at=expired_at))
            session.commit()

        history = MonitoringService(
            factory, clock=lambda: NOW
        ).history(hours=24)

        assert history.total_requests == 1
        assert len(history.resources) == 1
        assert len(parsed_values) == 2
    finally:
        engine.dispose()


def test_high_microsecond_timestamps_survive_cleanup_and_history(tmp_path):
    engine, factory = _factory(tmp_path)
    second = NOW - timedelta(seconds=1)
    try:
        with factory() as session:
            for microsecond in (999499, 999500, 999999):
                created_at = second.replace(microsecond=microsecond)
                session.add(
                    _metric(
                        f"high-microsecond-{microsecond}",
                        created_at=created_at,
                    )
                )
                session.add(_resource(created_at=created_at))
            session.commit()

        sampler = ResourceSampler(
            factory,
            ollama_status=lambda: None,
            inference_state=lambda: None,
            clock=lambda: NOW,
        )
        sampler._cleanup()
        history = MonitoringService(
            factory, clock=lambda: NOW
        ).history(hours=24)

        assert history.total_requests == 3
        assert len(history.resources) == 3
        assert {
            point.created_at.microsecond
            for point in history.resources
        } == {999499, 999500, 999999}
    finally:
        engine.dispose()


def test_history_discards_malformed_secret_bearing_metric_and_resource_rows(
    tmp_path, monkeypatch
):
    import app.services.monitoring as monitoring_module

    engine, factory = _factory(tmp_path)
    secret = "PASSWORD_SENTINEL http://internal-provider API_KEY_SENTINEL"
    schema_inputs = []
    real_resource_point = monitoring_module.MonitoringResourcePoint

    def guarded_resource_point(**values):
        schema_inputs.append(values)
        return real_resource_point(**values)

    monkeypatch.setattr(
        monitoring_module,
        "MonitoringResourcePoint",
        guarded_resource_point,
    )
    try:
        with factory() as session:
            session.add(_metric("valid", created_at=NOW))
            session.add(
                _metric(
                    "bad-feature",
                    created_at=NOW,
                    feature=secret,
                )
            )
            session.add(
                _metric(
                    "bad-mode",
                    created_at=NOW,
                    mode=secret,
                )
            )
            session.add(_resource(created_at=NOW))
            session.execute(
                text(
                    "INSERT INTO resource_samples "
                    "(id, cpu_percent, memory_percent, disk_percent, "
                    "ollama_available, model_loaded, running_count, "
                    "queued_count, created_at) "
                    "VALUES "
                    "(:id, :cpu, 20, 30, 0, 0, 0, 0, :created_at)"
                ),
                {
                    "id": "malformed-resource",
                    "cpu": secret,
                    "created_at": NOW,
                },
            )
            session.execute(
                text(
                    "INSERT INTO model_request_metrics "
                    "(id, feature, mode, model, request_id, operation, "
                    "queue_wait_ms, elapsed_ms, result_status, created_at) "
                    "VALUES "
                    "('malformed-metric-time', 'knowledge', 'local', "
                    "'local', 'malformed-time', 'knowledge_answer', "
                    "0, 1, 'error', :created_at)"
                ),
                {"created_at": secret},
            )
            session.execute(
                text(
                    "INSERT INTO resource_samples "
                    "(id, cpu_percent, memory_percent, disk_percent, "
                    "ollama_available, model_loaded, running_count, "
                    "queued_count, created_at) "
                    "VALUES "
                    "('malformed-resource-time', 10, 20, 30, "
                    "0, 0, 0, 0, :created_at)"
                ),
                {"created_at": secret},
            )
            session.commit()

        history = MonitoringService(
            factory, clock=lambda: NOW
        ).history(hours=24)
        serialized = json.dumps(history.model_dump(mode="json"))

        assert history.total_requests == 1
        assert len(history.requests) == 1
        assert len(history.resources) == 1
        assert all(secret not in values.values() for values in schema_inputs)
        assert "PASSWORD_SENTINEL" not in serialized
        assert "internal-provider" not in serialized
        assert "API_KEY_SENTINEL" not in serialized
    finally:
        engine.dispose()


def test_successful_sample_uses_psutil_and_cleans_expired_rows(
    tmp_path, monkeypatch
):
    import app.services.monitoring as monitoring_module

    engine, factory = _factory(tmp_path)
    try:
        with factory() as session:
            cutoff = NOW - timedelta(hours=24)
            session.add_all(
                [
                    _metric("expired", created_at=NOW - timedelta(hours=25)),
                    _metric("at-cutoff", created_at=cutoff),
                    _metric(
                        "before-cutoff-cleanup",
                        created_at=cutoff - timedelta(microseconds=1),
                    ),
                    _resource(created_at=NOW - timedelta(hours=25)),
                    _resource(created_at=cutoff),
                    _resource(
                        created_at=cutoff - timedelta(microseconds=1),
                    ),
                ]
            )
            secret = (
                "PASSWORD_SENTINEL http://internal-provider "
                "API_KEY_SENTINEL"
            )
            session.execute(
                text(
                    "INSERT INTO model_request_metrics "
                    "(id, feature, mode, model, request_id, operation, "
                    "queue_wait_ms, elapsed_ms, result_status, created_at) "
                    "VALUES "
                    "('invalid-cleanup-metric', 'knowledge', 'local', "
                    "'local', 'invalid-cleanup', 'knowledge_answer', "
                    "0, 1, 'error', :created_at)"
                ),
                {"created_at": secret},
            )
            session.execute(
                text(
                    "INSERT INTO resource_samples "
                    "(id, cpu_percent, memory_percent, disk_percent, "
                    "ollama_available, model_loaded, running_count, "
                    "queued_count, created_at) "
                    "VALUES "
                    "('invalid-cleanup-resource', 10, 20, 30, "
                    "0, 0, 0, 0, :created_at)"
                ),
                {"created_at": secret},
            )
            julian_future = session.scalar(
                text("SELECT julianday(:future)"),
                {"future": NOW + timedelta(days=365)},
            )
            session.execute(
                text(
                    "INSERT INTO model_request_metrics "
                    "(id, feature, mode, model, request_id, operation, "
                    "queue_wait_ms, elapsed_ms, result_status, created_at) "
                    "VALUES "
                    "('numeric-cleanup-metric', 'knowledge', 'local', "
                    "'local', 'numeric-cleanup', 'knowledge_answer', "
                    "0, 1, 'error', :created_at)"
                ),
                {"created_at": julian_future},
            )
            session.execute(
                text(
                    "INSERT INTO resource_samples "
                    "(id, cpu_percent, memory_percent, disk_percent, "
                    "ollama_available, model_loaded, running_count, "
                    "queued_count, created_at) "
                    "VALUES "
                    "('numeric-cleanup-resource', 10, 20, 30, "
                    "0, 0, 0, 0, :created_at)"
                ),
                {"created_at": julian_future},
            )
            for suffix, invalid_time in (
                ("hour-24", "2026-07-24 24:00:00.000000"),
                ("february-30", "2027-02-30 00:00:00.000000"),
            ):
                session.execute(
                    text(
                        "INSERT INTO model_request_metrics "
                        "(id, feature, mode, model, request_id, "
                        "operation, queue_wait_ms, elapsed_ms, "
                        "result_status, created_at) "
                        "VALUES "
                        "(:id, 'knowledge', 'local', 'local', "
                        ":request_id, 'knowledge_answer', 0, 1, "
                        "'error', :created_at)"
                    ),
                    {
                        "id": f"normalized-invalid-metric-{suffix}",
                        "request_id": (
                            f"normalized-invalid-{suffix}"
                        ),
                        "created_at": invalid_time,
                    },
                )
                session.execute(
                    text(
                        "INSERT INTO resource_samples "
                        "(id, cpu_percent, memory_percent, disk_percent, "
                        "ollama_available, model_loaded, running_count, "
                        "queued_count, created_at) "
                        "VALUES "
                        "(:id, 10, 20, 30, 0, 0, 0, 0, :created_at)"
                    ),
                    {
                        "id": f"normalized-invalid-resource-{suffix}",
                        "created_at": invalid_time,
                    },
                )
            session.commit()

        monkeypatch.setattr(monitoring_module.psutil, "cpu_percent", lambda: 11.0)
        monkeypatch.setattr(
            monitoring_module.psutil,
            "virtual_memory",
            lambda: type("Memory", (), {"percent": 22.0})(),
        )
        disk_paths = []

        def disk_usage(path):
            disk_paths.append(path)
            return type("Disk", (), {"percent": 33.0})()

        monkeypatch.setattr(monitoring_module.psutil, "disk_usage", disk_usage)
        sampler = ResourceSampler(
            factory,
            ollama_status=lambda: type(
                "Health",
                (),
                {"available": True, "loaded": True},
            )(),
            inference_state=lambda: type(
                "Inference",
                (),
                {"running": 2, "queued": 3},
            )(),
            clock=lambda: NOW,
        )

        sample = sampler.sample_once()

        assert sample is not None
        assert sample.cpu_percent == 11.0
        assert sample.running_count == 2
        assert sampler.status == "ok"
        assert disk_paths == ["/"]
        with factory() as session:
            stored = list(session.scalars(select(ResourceSample)))
            assert len(stored) == 2
            sampled = next(
                row for row in stored if row.created_at == NOW.replace(tzinfo=None)
            )
            assert sampled.cpu_percent == 11.0
            assert sampled.memory_percent == 22.0
            assert sampled.disk_percent == 33.0
            assert sampled.ollama_available
            assert sampled.model_loaded
            assert sampled.running_count == 2
            assert sampled.queued_count == 3
            metrics = list(session.scalars(select(ModelRequestMetric)))
            assert [metric.request_id for metric in metrics] == ["at-cutoff"]
            invalid_resources = session.scalar(
                text(
                    "SELECT count(*) FROM resource_samples "
                    "WHERE id = 'invalid-cleanup-resource'"
                )
            )
            invalid_metrics = session.scalar(
                text(
                    "SELECT count(*) FROM model_request_metrics "
                    "WHERE request_id = 'invalid-cleanup'"
                )
            )
            assert invalid_resources == 0
            assert invalid_metrics == 0
            numeric_resources = session.scalar(
                text(
                    "SELECT count(*) FROM resource_samples "
                    "WHERE id = 'numeric-cleanup-resource'"
                )
            )
            numeric_metrics = session.scalar(
                text(
                    "SELECT count(*) FROM model_request_metrics "
                    "WHERE request_id = 'numeric-cleanup'"
                )
            )
            assert numeric_resources == 0
            assert numeric_metrics == 0
            normalized_invalid_resources = session.scalar(
                text(
                    "SELECT count(*) FROM resource_samples "
                    "WHERE id LIKE 'normalized-invalid-resource-%'"
                )
            )
            normalized_invalid_metrics = session.scalar(
                text(
                    "SELECT count(*) FROM model_request_metrics "
                    "WHERE request_id LIKE 'normalized-invalid-%'"
                )
            )
            assert normalized_invalid_resources == 0
            assert normalized_invalid_metrics == 0
    finally:
        engine.dispose()


def test_sampling_failure_is_fixed_redacted_and_a_later_sample_recovers(
    tmp_path, monkeypatch
):
    import app.services.monitoring as monitoring_module

    engine, factory = _factory(tmp_path)
    secret = "sk-secret API_KEY=password cookie internal=http://private"
    calls = 0

    def cpu_percent():
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError(secret)
        return 11.0

    monkeypatch.setattr(monitoring_module.psutil, "cpu_percent", cpu_percent)
    monkeypatch.setattr(
        monitoring_module.psutil,
        "virtual_memory",
        lambda: type("Memory", (), {"percent": 22.0})(),
    )
    monkeypatch.setattr(
        monitoring_module.psutil,
        "disk_usage",
        lambda _: type("Disk", (), {"percent": 33.0})(),
    )
    sampler = ResourceSampler(
        factory,
        ollama_status=lambda: type(
            "Health", (), {"available": True, "loaded": True}
        )(),
        inference_state=lambda: type(
            "Inference", (), {"running": 0, "queued": 0}
        )(),
        clock=lambda: NOW,
    )
    try:
        assert sampler.sample_once() is None
        assert sampler.status == "sampling_error"
        assert secret not in repr(sampler.status)

        assert sampler.sample_once() is not None
        assert sampler.status == "ok"
        with factory() as session:
            assert len(list(session.scalars(select(ResourceSample)))) == 1
    finally:
        engine.dispose()


def test_sampler_start_stop_is_idempotent_daemon_and_prompt(tmp_path):
    engine, factory = _factory(tmp_path)
    sampler = ResourceSampler(
        factory,
        ollama_status=lambda: None,
        inference_state=lambda: None,
    )
    try:
        sampler.start()
        sampler.start()
        threads = [
            thread
            for thread in enumerate_threads()
            if thread.name == "resource-sampler"
        ]
        assert len(threads) == 1
        assert threads[0].daemon

        started = monotonic()
        sampler.stop()
        sampler.stop()

        assert monotonic() - started < 0.5
        assert not threads[0].is_alive()

        sampler.start()
        restarted = [
            thread
            for thread in enumerate_threads()
            if thread.name == "resource-sampler"
        ]
        assert len(restarted) == 1
        assert restarted[0] is not threads[0]
        sampler.stop()
        assert not restarted[0].is_alive()
    finally:
        sampler.stop()
        engine.dispose()


def test_start_during_stop_cannot_replace_worker_before_stop_returns(
    tmp_path,
):
    engine, factory = _factory(tmp_path)
    sampler = ResourceSampler(
        factory,
        ollama_status=lambda: None,
        inference_state=lambda: None,
    )
    allow_join = Event()
    join_entered = Event()
    sampler.start()
    old_worker = sampler._thread
    assert old_worker is not None
    original_join = old_worker.join

    def paused_join():
        join_entered.set()
        assert allow_join.wait(1)
        original_join()

    old_worker.join = paused_join  # type: ignore[method-assign]
    stop_thread = Thread(target=sampler.stop)
    try:
        stop_thread.start()
        assert join_entered.wait(1)
        original_join(timeout=1)
        assert not old_worker.is_alive()

        sampler.start()

        assert sampler._thread is old_worker
        allow_join.set()
        stop_thread.join(timeout=1)
        assert not stop_thread.is_alive()
        assert [
            thread
            for thread in enumerate_threads()
            if thread.name == "resource-sampler"
        ] == []
    finally:
        allow_join.set()
        stop_thread.join(timeout=1)
        sampler.stop()
        engine.dispose()


def test_thread_construction_failure_is_redacted_and_later_start_recovers(
    tmp_path, monkeypatch
):
    import app.services.monitoring as monitoring_module

    engine, factory = _factory(tmp_path)
    real_thread = monitoring_module.Thread

    def fail_thread_construction(**_):
        raise RuntimeError(
            "PASSWORD_SENTINEL API_KEY_SENTINEL http://internal-provider"
        )

    monkeypatch.setattr(
        monitoring_module,
        "Thread",
        fail_thread_construction,
    )
    sampler = ResourceSampler(
        factory,
        ollama_status=lambda: None,
        inference_state=lambda: None,
    )
    try:
        sampler.start()
        assert sampler.status == "sampling_error"
        assert [
            thread
            for thread in enumerate_threads()
            if thread.name == "resource-sampler"
        ] == []

        monkeypatch.setattr(monitoring_module, "Thread", real_thread)
        sampler.start()
        sampler.stop()
        assert [
            thread
            for thread in enumerate_threads()
            if thread.name == "resource-sampler"
        ] == []
    finally:
        sampler.stop()
        engine.dispose()


def test_thread_base_exception_is_not_swallowed(tmp_path, monkeypatch):
    import app.services.monitoring as monitoring_module

    engine, factory = _factory(tmp_path)

    def interrupt_thread_construction(**_):
        raise KeyboardInterrupt()

    monkeypatch.setattr(
        monitoring_module,
        "Thread",
        interrupt_thread_construction,
    )
    sampler = ResourceSampler(
        factory,
        ollama_status=lambda: None,
        inference_state=lambda: None,
    )
    try:
        with pytest.raises(KeyboardInterrupt):
            sampler.start()
    finally:
        sampler.stop()
        engine.dispose()


def test_thread_runtime_error_after_native_start_is_redacted_without_leak(
    tmp_path, monkeypatch
):
    import app.services.monitoring as monitoring_module

    engine, factory = _factory(tmp_path)

    class StartsThenFails(Thread):
        def start(self):
            super().start()
            raise RuntimeError(
                "PASSWORD_SENTINEL http://internal-provider"
            )

    monkeypatch.setattr(
        monitoring_module,
        "Thread",
        StartsThenFails,
    )
    sampler = ResourceSampler(
        factory,
        ollama_status=lambda: None,
        inference_state=lambda: None,
    )
    try:
        sampler.start()

        assert sampler.status == "sampling_error"
        assert [
            thread
            for thread in enumerate_threads()
            if thread.name == "resource-sampler"
        ] == []
    finally:
        sampler.stop()
        engine.dispose()


def test_restart_cannot_replace_worker_during_failed_start_cleanup(
    tmp_path, monkeypatch
):
    import app.services.monitoring as monitoring_module

    engine, factory = _factory(tmp_path)
    constructed = []

    class StartsThenFails(Thread):
        def run(self):
            return

        def start(self):
            super().start()
            self.join()
            raise RuntimeError("worker start failed")

    def thread_factory(**kwargs):
        if not constructed:
            thread = StartsThenFails(**kwargs)
        else:
            thread = Thread(**kwargs)
        constructed.append(thread)
        return thread

    monkeypatch.setattr(
        monitoring_module,
        "Thread",
        thread_factory,
    )
    sampler = ResourceSampler(
        factory,
        ollama_status=lambda: None,
        inference_state=lambda: None,
    )
    cleanup_entered = Event()
    allow_cleanup = Event()
    original_cleanup = sampler._cleanup_failed_start

    def paused_cleanup(thread):
        cleanup_entered.set()
        assert allow_cleanup.wait(1)
        original_cleanup(thread)

    sampler._cleanup_failed_start = paused_cleanup
    start_thread = Thread(target=sampler.start)
    try:
        start_thread.start()
        assert cleanup_entered.wait(1)
        failed_worker = constructed[0]
        assert not failed_worker.is_alive()

        sampler.start()

        assert constructed == [failed_worker]
        assert sampler._thread is failed_worker
        allow_cleanup.set()
        start_thread.join(timeout=1)
        assert not start_thread.is_alive()
        assert sampler._thread is None
    finally:
        allow_cleanup.set()
        start_thread.join(timeout=1)
        sampler.stop()
        engine.dispose()


def test_thread_base_exception_after_native_start_cleans_before_reraise(
    tmp_path, monkeypatch
):
    import app.services.monitoring as monitoring_module

    engine, factory = _factory(tmp_path)

    class StartsThenInterrupts(Thread):
        def start(self):
            super().start()
            raise KeyboardInterrupt()

    monkeypatch.setattr(
        monitoring_module,
        "Thread",
        StartsThenInterrupts,
    )
    sampler = ResourceSampler(
        factory,
        ollama_status=lambda: None,
        inference_state=lambda: None,
    )
    try:
        with pytest.raises(KeyboardInterrupt):
            sampler.start()

        assert [
            thread
            for thread in enumerate_threads()
            if thread.name == "resource-sampler"
        ] == []
    finally:
        sampler.stop()
        engine.dispose()


def test_sampler_stop_waits_for_an_active_sample_and_leaves_no_thread(tmp_path):
    engine, factory = _factory(tmp_path)
    sampling = Event()
    release = Event()

    def blocked_status():
        sampling.set()
        release.wait()
        return None

    sampler = ResourceSampler(
        factory,
        ollama_status=blocked_status,
        inference_state=lambda: None,
        interval_seconds=0.01,
    )
    stop_thread = Thread(target=sampler.stop)
    try:
        sampler.start()
        assert sampling.wait(1)
        stop_thread.start()
        stop_thread.join(timeout=0.05)
        assert stop_thread.is_alive()
        release.set()
        stop_thread.join(timeout=1)
        assert not stop_thread.is_alive()
        assert [
            thread
            for thread in enumerate_threads()
            if thread.name == "resource-sampler"
        ] == []
    finally:
        release.set()
        stop_thread.join(timeout=1)
        sampler.stop()
        engine.dispose()


def test_monitoring_payloads_do_not_serialize_sensitive_content(tmp_path):
    engine, factory = _factory(tmp_path)
    sentinels = [
        "PROMPT_SENTINEL",
        "DOCUMENT_SENTINEL",
        "PASSWORD_SENTINEL",
        "COOKIE_SENTINEL",
        "API_KEY_SENTINEL",
        "http://internal-provider:11434",
        "RAW_PROVIDER_ERROR_SENTINEL",
    ]
    try:
        with factory() as session:
            metric = _metric(
                "safe-id",
                created_at=NOW,
                feature="knowledge",
                mode="local",
                result_status=" ".join(sentinels),
            )
            metric.model = " ".join(sentinels)
            metric.operation = " ".join(sentinels)
            session.add(metric)
            session.add(_resource(created_at=NOW))
            session.commit()

        payload = MonitoringService(
            factory, clock=lambda: NOW
        ).history(hours=24).model_dump(mode="json")
        serialized = json.dumps(payload)

        for sentinel in sentinels:
            assert sentinel not in serialized
        assert payload["requests"][0]["result_status"] == "error"
        assert set(payload) == {
            "hours",
            "total_requests",
            "average_elapsed_ms",
            "p95_elapsed_ms",
            "average_ttft_ms",
            "average_queue_wait_ms",
            "max_queue_depth",
            "input_tokens",
            "output_tokens",
            "requests",
            "resources",
        }
    finally:
        engine.dispose()
