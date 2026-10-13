from asyncio import CancelledError
from concurrent.futures import ThreadPoolExecutor
from threading import (
    Barrier,
    BrokenBarrierError,
    Event,
    Thread,
    Timer,
    local,
)
from time import monotonic, sleep

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event, select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

import app.services.inference as inference_module
from app.config import Settings
from app.main import create_app
from app.models import Base, ModelRequestMetric
from app.providers.feature_router import FeatureProviderSnapshot
from app.providers.base import ProviderUsage
from app.providers.mock import MockProvider
from app.providers.ollama import OllamaClient
from app.services.inference import (
    InferenceCoordinator,
    InferenceQueueFull,
    InferenceTimedOut,
    _StreamState,
)


def _snapshot(
    mode: str = "local",
    feature: str = "knowledge",
    model: str | None = None,
    provider=None,
) -> FeatureProviderSnapshot:
    return FeatureProviderSnapshot(
        feature=feature,
        mode=mode,
        model=(model or "qwen3:test") if mode == "local" else None,
        provider=provider or MockProvider(delay_ms=0),
    )


class ThreadUsageProvider(MockProvider):
    def __init__(self):
        super().__init__(delay_ms=0)
        self._usage = local()

    @property
    def last_usage(self) -> ProviderUsage:
        return getattr(
            self._usage,
            "value",
            ProviderUsage(input_tokens=None, output_tokens=None),
        )

    def reset_usage(self) -> None:
        self._usage.value = ProviderUsage(
            input_tokens=None, output_tokens=None
        )

    def set_usage(self, input_tokens: int, output_tokens: int) -> None:
        self._usage.value = ProviderUsage(
            input_tokens=input_tokens,
            output_tokens=output_tokens,
        )


@pytest.fixture
def metric_store(tmp_path):
    database_path = tmp_path / "metrics.db"
    engine = create_engine(
        f"sqlite:///{database_path}",
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine)
    try:
        yield factory, database_path
    finally:
        engine.dispose()


def _wait_until(predicate, timeout: float = 1.0) -> None:
    deadline = monotonic() + timeout
    while not predicate():
        if monotonic() >= deadline:
            raise AssertionError("condition was not met before deadline")
        sleep(0.001)


def _metric_count(factory, result_status: str | None = None) -> int:
    with factory() as session:
        statement = select(ModelRequestMetric)
        if result_status is not None:
            statement = statement.where(
                ModelRequestMetric.result_status == result_status
            )
        return len(list(session.scalars(statement)))


def _capture_error(errors, callback) -> None:
    try:
        callback()
    except BaseException as exc:
        errors.append(exc)


def test_local_gate_allows_one_running_and_three_waiting(metric_store):
    factory, _ = metric_store
    coordinator = InferenceCoordinator(
        factory, concurrency=1, max_queue=3, timeout_seconds=2
    )
    blocker = Event()
    callbacks_started = 0
    provider = ThreadUsageProvider()
    provider.set_usage(91, 37)
    snapshot = _snapshot(provider=provider)

    def hold_gate():
        nonlocal callbacks_started
        callbacks_started += 1
        assert blocker.wait(1)
        return "running"

    errors = []

    def run_owner():
        try:
            coordinator.execute(
                "knowledge", "knowledge_answer", snapshot, hold_gate
            )
        except BaseException as exc:
            errors.append(exc)

    running = Thread(target=run_owner)
    running.start()
    _wait_until(lambda: coordinator.live_state().running == 1)

    try:
        with ThreadPoolExecutor(max_workers=3) as pool:
            queued = [
                pool.submit(
                    coordinator.execute,
                    "knowledge",
                    "knowledge_answer",
                    snapshot,
                    lambda: "queued",
                )
                for _ in range(3)
            ]
            _wait_until(lambda: coordinator.live_state().queued == 3)

            with pytest.raises(InferenceQueueFull):
                coordinator.execute(
                    "knowledge",
                    "knowledge_answer",
                    snapshot,
                    lambda: "rejected",
                )

            assert coordinator.live_state().running == 1
            assert coordinator.live_state().queued == 3
            assert callbacks_started == 1
            blocker.set()
            assert [future.result(timeout=1) for future in queued] == [
                "queued",
                "queued",
                "queued",
            ]
    finally:
        blocker.set()
        running.join(timeout=1)

    assert not running.is_alive()
    assert errors == []
    _wait_until(lambda: coordinator.live_state().running == 0)
    assert coordinator.live_state().running == 0
    assert coordinator.live_state().queued == 0
    _wait_until(lambda: _metric_count(factory) == 5)
    with factory() as session:
        statuses = list(
            session.scalars(
                select(ModelRequestMetric.result_status).order_by(
                    ModelRequestMetric.created_at
                )
            )
        )
        assert statuses.count("queue_full") == 1
        assert statuses.count("success") == 4
        queue_full = session.scalar(
            select(ModelRequestMetric).where(
                ModelRequestMetric.result_status == "queue_full"
            )
        )
        assert queue_full is not None
        assert queue_full.input_tokens is None
        assert queue_full.output_tokens is None


def test_execution_timeout_uses_total_monotonic_deadline_and_releases_gate(
    metric_store,
):
    factory, _ = metric_store
    ticks = iter((10.0, 10.0, 10.2, 10.2, 10.2, 10.2))
    coordinator = InferenceCoordinator(
        factory,
        concurrency=1,
        max_queue=3,
        timeout_seconds=0.1,
        clock=lambda: next(ticks),
    )

    with pytest.raises(InferenceTimedOut):
        coordinator.execute(
            "knowledge",
            "knowledge_answer",
            _snapshot(),
            lambda: "too late",
        )

    _wait_until(lambda: coordinator.live_state().running == 0)
    assert coordinator.live_state().running == 0
    assert coordinator.live_state().queued == 0


def test_blocking_execute_times_out_but_holds_slot_until_worker_finishes(
    metric_store,
):
    factory, _ = metric_store
    coordinator = InferenceCoordinator(
        factory, concurrency=1, max_queue=3, timeout_seconds=0.03
    )
    callback_started = Event()
    release_callback = Event()
    second_callback_started = Event()
    safety = Timer(0.3, release_callback.set)
    safety.start()

    def block():
        callback_started.set()
        release_callback.wait()
        return "late"

    started_at = monotonic()
    try:
        with pytest.raises(InferenceTimedOut):
            coordinator.execute(
                "knowledge", "knowledge_answer", _snapshot(), block
            )
        elapsed = monotonic() - started_at

        assert elapsed < 0.15
        assert callback_started.is_set()
        assert coordinator.live_state().running == 1

        with pytest.raises(InferenceTimedOut):
            coordinator.execute(
                "knowledge",
                "knowledge_answer",
                _snapshot(),
                second_callback_started.set,
            )
        assert not second_callback_started.is_set()
        assert coordinator.live_state().running == 1
    finally:
        release_callback.set()
        safety.cancel()

    _wait_until(lambda: coordinator.live_state().running == 0)
    assert coordinator.live_state().queued == 0
    _wait_until(
        lambda: _metric_count(factory, result_status="timeout") == 2
    )


def test_execute_timeout_metric_is_immediate_and_never_duplicated(metric_store):
    factory, _ = metric_store
    coordinator = InferenceCoordinator(factory, timeout_seconds=0.03)
    callback_started = Event()
    release_callback = Event()
    safety = Timer(0.3, release_callback.set)
    safety.start()

    def block():
        callback_started.set()
        release_callback.wait()

    try:
        with pytest.raises(InferenceTimedOut):
            coordinator.execute(
                "knowledge", "knowledge_answer", _snapshot(), block
            )

        assert callback_started.is_set()
        assert coordinator.live_state().running == 1
        _wait_until(
            lambda: _metric_count(
                factory, result_status="timeout"
            )
            == 1
        )
        assert _metric_count(factory, result_status="timeout") == 1
    finally:
        release_callback.set()
        safety.cancel()

    _wait_until(lambda: coordinator.live_state().running == 0)
    assert coordinator.shutdown(timeout_seconds=0.1) is True
    assert _metric_count(factory) == 1


def test_immediate_free_slot_checks_deadline_before_starting_callback(metric_store):
    factory, _ = metric_store
    ticks = iter((40.0, 40.2, 40.2, 40.2))
    callback_started = Event()
    coordinator = InferenceCoordinator(
        factory,
        timeout_seconds=0.1,
        clock=lambda: next(ticks),
    )

    with pytest.raises(InferenceTimedOut):
        coordinator.execute(
            "knowledge",
            "knowledge_answer",
            _snapshot(),
            callback_started.set,
        )

    assert not callback_started.is_set()
    assert coordinator.live_state().running == 0
    assert coordinator.live_state().queued == 0


def test_execute_thread_constructor_failure_restores_slot_and_records_once(
    metric_store, monkeypatch
):
    factory, _ = metric_store
    coordinator = InferenceCoordinator(factory)

    def fail_constructor(**_):
        raise MemoryError("thread allocation failed")

    monkeypatch.setattr(inference_module, "Thread", fail_constructor)

    with pytest.raises(MemoryError, match="allocation"):
        coordinator.execute(
            "knowledge",
            "knowledge_answer",
            _snapshot(),
            lambda: None,
        )

    assert coordinator.live_state().running == 0
    assert coordinator.live_state().queued == 0
    _wait_until(
        lambda: _metric_count(factory, result_status="error") == 1
    )
    assert _metric_count(factory, result_status="error") == 1


def test_stream_thread_constructor_failure_restores_slot_and_records_once(
    metric_store, monkeypatch
):
    factory, _ = metric_store
    coordinator = InferenceCoordinator(factory)

    def fail_constructor(**_):
        raise MemoryError("stream thread allocation failed")

    monkeypatch.setattr(inference_module, "Thread", fail_constructor)
    stream = coordinator.stream(
        "call_assistant",
        "call_suggestion",
        _snapshot(feature="call_assistant"),
        lambda: iter(("never",)),
    )

    with pytest.raises(MemoryError, match="allocation"):
        next(stream)

    assert coordinator.live_state().running == 0
    assert coordinator.live_state().queued == 0
    _wait_until(
        lambda: _metric_count(factory, result_status="error") == 1
    )
    assert _metric_count(factory, result_status="error") == 1


def test_ambiguous_execute_start_aborts_worker_without_double_release(
    metric_store, monkeypatch
):
    factory, _ = metric_store
    coordinator = InferenceCoordinator(factory)
    callback_started = Event()

    class StartsThenRaises(Thread):
        def start(self):
            super().start()
            raise RuntimeError("ambiguous native start")

    monkeypatch.setattr(inference_module, "Thread", StartsThenRaises)

    with pytest.raises(RuntimeError, match="ambiguous"):
        coordinator.execute(
            "knowledge",
            "knowledge_answer",
            _snapshot(),
            callback_started.set,
        )

    _wait_until(lambda: coordinator.live_state().running == 0)
    _wait_until(lambda: _metric_count(factory) == 1)
    assert not callback_started.is_set()
    assert coordinator.live_state().running == 0
    assert coordinator.live_state().queued == 0
    assert _metric_count(factory, result_status="error") == 1


def test_ambiguous_stream_start_aborts_factory_without_double_release(
    metric_store, monkeypatch
):
    factory, _ = metric_store
    coordinator = InferenceCoordinator(factory)
    factory_started = Event()

    class StartsThenRaises(Thread):
        def start(self):
            super().start()
            raise RuntimeError("ambiguous stream start")

    monkeypatch.setattr(inference_module, "Thread", StartsThenRaises)
    stream = coordinator.stream(
        "call_assistant",
        "call_suggestion",
        _snapshot(feature="call_assistant"),
        lambda: factory_started.set() or iter(("never",)),
    )

    with pytest.raises(RuntimeError, match="ambiguous"):
        next(stream)

    _wait_until(lambda: coordinator.live_state().running == 0)
    _wait_until(lambda: _metric_count(factory) == 1)
    assert not factory_started.is_set()
    assert coordinator.live_state().running == 0
    assert coordinator.live_state().queued == 0
    assert _metric_count(factory, result_status="error") == 1


def test_timeout_while_queued_decrements_queue_and_records_wait(metric_store):
    factory, _ = metric_store
    coordinator = InferenceCoordinator(
        factory, concurrency=1, max_queue=3, timeout_seconds=0.03
    )
    blocker = Event()
    owner_started = Event()
    provider = ThreadUsageProvider()
    provider.set_usage(81, 29)
    snapshot = _snapshot(provider=provider)

    def hold_gate():
        owner_started.set()
        assert blocker.wait(1)

    owner_errors = []

    def run_owner():
        try:
            coordinator.execute(
                "knowledge", "knowledge_answer", snapshot, hold_gate
            )
        except InferenceTimedOut as exc:
            owner_errors.append(exc)

    owner = Thread(target=run_owner)
    owner.start()
    assert owner_started.wait(1)

    try:
        with pytest.raises(InferenceTimedOut):
            coordinator.execute(
                "knowledge", "knowledge_answer", snapshot, lambda: None
            )
        assert coordinator.live_state().running == 1
        assert coordinator.live_state().queued == 0
    finally:
        blocker.set()
        owner.join(timeout=1)

    assert not owner.is_alive()
    assert len(owner_errors) == 1
    _wait_until(lambda: coordinator.live_state().running == 0)
    assert coordinator.live_state().running == 0
    assert coordinator.live_state().queued == 0
    _wait_until(
        lambda: (
            _metric_count(factory, result_status="timeout") == 2
        )
    )
    with factory() as session:
        metric = session.scalar(
            select(ModelRequestMetric).where(
                ModelRequestMetric.result_status == "timeout",
                ModelRequestMetric.queue_wait_ms > 0,
            )
        )
        assert metric is not None
        assert metric.result_status == "timeout"
        assert metric.queue_wait_ms > 0
        assert metric.input_tokens is None
        assert metric.output_tokens is None


def test_waiter_notified_after_deadline_never_starts_provider_callback(metric_store):
    factory, _ = metric_store
    now = [30.0]
    coordinator = InferenceCoordinator(
        factory,
        concurrency=1,
        max_queue=3,
        timeout_seconds=0.1,
        clock=lambda: now[0],
    )
    owner_started = Event()
    release_owner = Event()
    waiter_callback_started = Event()
    errors = []

    def owner_callback():
        owner_started.set()
        assert release_owner.wait(1)

    def run(operation, callback):
        try:
            coordinator.execute(
                "knowledge", "knowledge_answer", _snapshot(), callback
            )
        except InferenceTimedOut as exc:
            errors.append(exc)

    owner = Thread(target=run, args=("knowledge_answer", owner_callback))
    waiter = Thread(
        target=run,
        args=("knowledge_answer", waiter_callback_started.set),
    )
    owner.start()
    assert owner_started.wait(1)
    waiter.start()
    _wait_until(lambda: coordinator.live_state().queued == 1)

    now[0] = 30.2
    release_owner.set()
    owner.join(timeout=1)
    waiter.join(timeout=1)

    assert not owner.is_alive()
    assert not waiter.is_alive()
    assert len(errors) == 2
    assert not waiter_callback_started.is_set()
    assert coordinator.live_state().running == 0
    assert coordinator.live_state().queued == 0


def test_provider_error_releases_gate_and_records_error(metric_store):
    factory, _ = metric_store
    coordinator = InferenceCoordinator(factory)

    def fail():
        raise RuntimeError("provider payload must not be recorded")

    with pytest.raises(RuntimeError, match="provider payload"):
        coordinator.execute(
            "knowledge", "knowledge_answer", _snapshot(), fail
        )

    assert coordinator.live_state().running == 0
    assert coordinator.live_state().queued == 0
    _wait_until(lambda: _metric_count(factory) == 1)
    with factory() as session:
        metric = session.scalar(select(ModelRequestMetric))
        assert metric is not None
        assert metric.result_status == "error"


def test_pre_provider_failure_resets_stale_usage_for_this_request(metric_store):
    factory, _ = metric_store
    provider = ThreadUsageProvider()
    provider.set_usage(73, 31)
    coordinator = InferenceCoordinator(factory)

    def fail_before_provider():
        raise RuntimeError("failed before provider invocation")

    with pytest.raises(RuntimeError, match="before provider"):
        coordinator.execute(
            "knowledge",
            "knowledge_answer",
            _snapshot(provider=provider),
            fail_before_provider,
        )

    _wait_until(lambda: _metric_count(factory) == 1)
    with factory() as session:
        metric = session.scalar(select(ModelRequestMetric))
        assert metric is not None
        assert metric.input_tokens is None
        assert metric.output_tokens is None


def test_worker_captures_usage_from_the_same_request_thread(metric_store):
    factory, _ = metric_store
    provider = ThreadUsageProvider()
    coordinator = InferenceCoordinator(factory)

    def complete_with_usage():
        provider.set_usage(23, 11)
        return "answer"

    assert coordinator.execute(
        "knowledge",
        "knowledge_answer",
        _snapshot(provider=provider),
        complete_with_usage,
    ) == "answer"

    _wait_until(lambda: _metric_count(factory) == 1)
    with factory() as session:
        metric = session.scalar(select(ModelRequestMetric))
        assert metric is not None
        assert metric.input_tokens == 23
        assert metric.output_tokens == 11


def test_callback_cancellation_releases_gate_and_records_cancellation(metric_store):
    factory, _ = metric_store
    coordinator = InferenceCoordinator(factory)

    def cancel():
        raise CancelledError()

    with pytest.raises(CancelledError):
        coordinator.execute(
            "knowledge", "knowledge_answer", _snapshot(), cancel
        )

    assert coordinator.live_state().running == 0
    assert coordinator.live_state().queued == 0
    _wait_until(lambda: _metric_count(factory) == 1)
    with factory() as session:
        metric = session.scalar(select(ModelRequestMetric))
        assert metric is not None
        assert metric.result_status == "cancelled"


def test_stream_signal_cancelled_before_admission_never_invokes_provider(
    metric_store,
):
    factory, _ = metric_store
    coordinator = InferenceCoordinator(factory)
    cancellation = inference_module.InferenceCancellation()
    provider_calls = []
    cancellation.cancel()
    stream = coordinator.stream(
        "knowledge",
        "knowledge_answer",
        _snapshot(),
        lambda: provider_calls.append("called") or iter(("never",)),
        cancellation=cancellation,
    )

    with pytest.raises(inference_module.InferenceCancelled):
        next(stream)

    assert provider_calls == []
    assert coordinator.live_state().running == 0
    assert coordinator.live_state().queued == 0
    _wait_until(
        lambda: _metric_count(factory, result_status="cancelled") == 1
    )
    assert _metric_count(factory) == 1


def test_pre_cancelled_stream_wins_before_zero_capacity_queue_rejection(
    metric_store,
):
    factory, _ = metric_store
    coordinator = InferenceCoordinator(
        factory,
        max_queue=0,
        timeout_seconds=2,
    )
    owner_release = Event()
    provider_calls = []

    def owner_source():
        yield "owner"
        assert owner_release.wait(1)

    owner = coordinator.stream(
        "knowledge",
        "knowledge_answer",
        _snapshot(),
        owner_source,
    )
    assert next(owner) == "owner"
    cancellation = inference_module.InferenceCancellation()
    assert cancellation.cancel() is True
    assert cancellation.cancel() is False
    cancelled = coordinator.stream(
        "knowledge",
        "knowledge_answer",
        _snapshot(),
        lambda: provider_calls.append("called") or iter(("never",)),
        cancellation=cancellation,
    )

    try:
        with pytest.raises(inference_module.InferenceCancelled):
            next(cancelled)
        assert provider_calls == []
        assert coordinator.live_state().running == 1
        assert coordinator.live_state().queued == 0
        _wait_until(
            lambda: _metric_count(
                factory, result_status="cancelled"
            )
            == 1
        )
    finally:
        owner_release.set()
        list(owner)
        cancelled.close()
        assert coordinator.shutdown(timeout_seconds=1)


def test_cancelling_queued_stream_wakes_without_touching_running_owner(
    metric_store,
):
    factory, database_path = metric_store
    coordinator = InferenceCoordinator(factory, timeout_seconds=2)
    owner_release = Event()
    owner_blocked = Event()
    queued_provider_calls = []
    queued_errors = []
    secret_question = "SECRET queued question must never persist"

    def owner_source():
        yield "owner"
        owner_blocked.set()
        assert owner_release.wait(1)

    owner = coordinator.stream(
        "knowledge",
        "knowledge_answer",
        _snapshot(),
        owner_source,
    )
    assert next(owner) == "owner"
    assert owner_blocked.wait(1)
    cancellation = inference_module.InferenceCancellation()
    queued = coordinator.stream(
        "knowledge",
        "knowledge_answer",
        _snapshot(),
        lambda: queued_provider_calls.append(secret_question)
        or iter(("never",)),
        cancellation=cancellation,
    )
    queued_thread = Thread(
        target=lambda: _capture_error(
            queued_errors, lambda: next(queued)
        )
    )
    queued_thread.start()
    _wait_until(lambda: coordinator.live_state().queued == 1)

    cancellation.cancel()
    queued_thread.join(timeout=0.2)

    try:
        assert not queued_thread.is_alive()
        assert queued_provider_calls == []
        assert coordinator.live_state().running == 1
        assert coordinator.live_state().queued == 0
        assert len(queued_errors) == 1
        assert isinstance(
            queued_errors[0], inference_module.InferenceCancelled
        )
        _wait_until(
            lambda: _metric_count(
                factory, result_status="cancelled"
            )
            == 1
        )
        assert _metric_count(factory) == 1
        assert secret_question.encode() not in database_path.read_bytes()
    finally:
        owner_release.set()
        list(owner)
        queued.close()
        coordinator.shutdown(timeout_seconds=1)


def test_cancel_after_admission_before_worker_skips_provider_and_releases_once(
    metric_store,
    monkeypatch,
):
    factory, _ = metric_store
    coordinator = InferenceCoordinator(factory)
    cancellation = inference_module.InferenceCancellation()
    admission_authorized = Event()
    release_acquire = Event()
    provider_calls = []
    errors = []
    original_acquire = coordinator._acquire

    def pause_after_admission(started_at, deadline, cancellation=None):
        waited = original_acquire(
            started_at,
            deadline,
            cancellation=cancellation,
        )
        admission_authorized.set()
        assert release_acquire.wait(1)
        return waited

    monkeypatch.setattr(coordinator, "_acquire", pause_after_admission)
    stream = coordinator.stream(
        "knowledge",
        "knowledge_answer",
        _snapshot(),
        lambda: provider_calls.append("called") or iter(("never",)),
        cancellation=cancellation,
    )
    consumer = Thread(
        target=lambda: _capture_error(errors, lambda: next(stream))
    )
    consumer.start()
    assert admission_authorized.wait(1)
    assert coordinator.live_state().running == 1

    cancellation.cancel()
    release_acquire.set()
    consumer.join(timeout=1)

    assert not consumer.is_alive()
    assert provider_calls == []
    assert len(errors) == 1
    assert isinstance(errors[0], inference_module.InferenceCancelled)
    _wait_until(lambda: coordinator.live_state().running == 0)
    assert coordinator.live_state().queued == 0
    _wait_until(
        lambda: _metric_count(factory, result_status="cancelled") == 1
    )
    assert _metric_count(factory) == 1
    stream.close()
    assert coordinator.shutdown(timeout_seconds=0.1)


def test_cancel_at_provider_start_claim_prevents_factory_invocation(
    metric_store,
):
    factory, _ = metric_store
    claim_reached = Event()
    release_claim = Event()
    provider_calls = []
    errors = []

    class PausingCancellation(
        inference_module.InferenceCancellation
    ):
        def claim_provider_start(self):
            claim_reached.set()
            assert release_claim.wait(1)
            return super().claim_provider_start()

    coordinator = InferenceCoordinator(factory)
    cancellation = PausingCancellation()
    stream = coordinator.stream(
        "knowledge",
        "knowledge_answer",
        _snapshot(),
        lambda: provider_calls.append("called") or iter(("never",)),
        cancellation=cancellation,
    )
    consumer = Thread(
        target=lambda: _capture_error(errors, lambda: list(stream))
    )
    consumer.start()

    try:
        assert claim_reached.wait(0.5)
        assert cancellation.cancel() is True
        release_claim.set()
        consumer.join(timeout=1)

        assert not consumer.is_alive()
        assert provider_calls == []
        assert len(errors) == 1
        assert isinstance(
            errors[0], inference_module.InferenceCancelled
        )
        _wait_until(lambda: coordinator.live_state().running == 0)
        assert coordinator.live_state().queued == 0
        _wait_until(
            lambda: _metric_count(
                factory, result_status="cancelled"
            )
            == 1
        )
        assert _metric_count(factory) == 1
    finally:
        release_claim.set()
        consumer.join(timeout=1)
        stream.close()
        coordinator.shutdown(timeout_seconds=1)


def test_provider_start_claim_preserves_the_first_linearization_winner():
    provider_first = inference_module.InferenceCancellation()
    assert provider_first.claim_provider_start() is True
    assert provider_first.cancel() is True
    assert provider_first.claim_provider_start() is True
    assert provider_first.provider_start_won() is True

    cancellation_first = inference_module.InferenceCancellation()
    assert cancellation_first.cancel() is True
    assert cancellation_first.claim_provider_start() is False
    assert cancellation_first.claim_provider_start() is False
    assert cancellation_first.provider_start_won() is False


def test_provider_start_cancellation_race_records_one_winner(
    metric_store,
):
    factory, _ = metric_store
    for _ in range(25):
        gate = Barrier(2)
        provider_calls = []
        errors = []

        class RacingCancellation(
            inference_module.InferenceCancellation
        ):
            def claim_provider_start(self):
                try:
                    gate.wait(timeout=1)
                except BrokenBarrierError:
                    pass
                return super().claim_provider_start()

        coordinator = InferenceCoordinator(factory)
        cancellation = RacingCancellation()
        stream = coordinator.stream(
            "knowledge",
            "knowledge_answer",
            _snapshot(),
            lambda: provider_calls.append("called") or iter(()),
            cancellation=cancellation,
        )
        consumer = Thread(
            target=lambda: _capture_error(
                errors, lambda: list(stream)
            )
        )

        def cancel_at_gate():
            try:
                gate.wait(timeout=0.5)
            except BrokenBarrierError:
                return
            cancellation.cancel()

        canceller = Thread(target=cancel_at_gate)
        consumer.start()
        canceller.start()
        consumer.join(timeout=1)
        canceller.join(timeout=1)

        try:
            assert not consumer.is_alive()
            assert not canceller.is_alive()
            assert cancellation.is_cancelled()
            if cancellation.provider_start_won():
                assert provider_calls == ["called"]
            else:
                assert provider_calls == []
            _wait_until(
                lambda: coordinator.live_state().running == 0
            )
            assert coordinator.live_state().queued == 0
            assert all(
                isinstance(
                    error,
                    inference_module.InferenceCancelled,
                )
                for error in errors
            )
        finally:
            stream.close()
            coordinator.shutdown(timeout_seconds=1)


def test_stream_cancellation_admission_race_has_one_owner_and_no_double_release(
    metric_store,
):
    factory, _ = metric_store
    for _ in range(25):
        coordinator = InferenceCoordinator(factory, timeout_seconds=2)
        owner_release = Event()

        def owner_source():
            yield "owner"
            assert owner_release.wait(1)

        owner = coordinator.stream(
            "knowledge",
            "knowledge_answer",
            _snapshot(),
            owner_source,
        )
        assert next(owner) == "owner"
        cancellation = inference_module.InferenceCancellation()
        provider_calls = []
        queued_errors = []
        queued = coordinator.stream(
            "knowledge",
            "knowledge_answer",
            _snapshot(),
            lambda: provider_calls.append("called")
            or iter(("provider",)),
            cancellation=cancellation,
        )
        consumer = Thread(
            target=lambda: _capture_error(
                queued_errors, lambda: list(queued)
            )
        )
        consumer.start()
        _wait_until(lambda: coordinator.live_state().queued == 1)
        race = Barrier(3)
        release_thread = Thread(
            target=lambda: (race.wait(), owner_release.set())
        )
        cancel_thread = Thread(
            target=lambda: (race.wait(), cancellation.cancel())
        )
        release_thread.start()
        cancel_thread.start()
        race.wait()
        release_thread.join(timeout=1)
        cancel_thread.join(timeout=1)
        consumer.join(timeout=1)
        list(owner)
        _wait_until(
            lambda: coordinator.live_state().running == 0
            and coordinator.live_state().queued == 0
        )

        assert not consumer.is_alive()
        assert len(provider_calls) <= 1
        if not cancellation.admission_won():
            assert provider_calls == []
        assert coordinator.live_state().running == 0
        assert coordinator.live_state().queued == 0
        assert all(
            isinstance(
                error,
                (
                    inference_module.InferenceCancelled,
                    StopIteration,
                ),
            )
            for error in queued_errors
        )
        queued.close()
        owner.close()
        assert coordinator.shutdown(timeout_seconds=0.2)


def test_closing_stream_closes_upstream_releases_gate_and_records_cancellation(
    metric_store,
):
    factory, _ = metric_store
    coordinator = InferenceCoordinator(factory)
    upstream_closed = Event()

    def source():
        try:
            yield "first"
            yield "second"
        finally:
            upstream_closed.set()

    stream = coordinator.stream(
        "call_assistant",
        "call_suggestion",
        _snapshot(feature="call_assistant"),
        lambda: source(),
    )
    assert next(stream) == "first"
    assert coordinator.live_state().running == 1

    stream.close()

    _wait_until(upstream_closed.is_set)
    _wait_until(lambda: coordinator.live_state().running == 0)
    _wait_until(lambda: _metric_count(factory) == 1)
    assert upstream_closed.is_set()
    assert coordinator.live_state().running == 0
    assert coordinator.live_state().queued == 0
    with factory() as session:
        metric = session.scalar(select(ModelRequestMetric))
        assert metric is not None
        assert metric.result_status == "cancelled"
        assert metric.ttft_ms is not None


def test_consumer_close_metric_is_immediate_while_worker_retains_slot(
    metric_store,
):
    factory, _ = metric_store
    coordinator = InferenceCoordinator(factory)
    second_next_started = Event()
    release_second_next = Event()
    close_called = Event()
    safety = Timer(0.3, release_second_next.set)
    safety.start()

    class BlockingAfterFirst:
        def __init__(self):
            self._first = True

        def __iter__(self):
            return self

        def __next__(self):
            if self._first:
                self._first = False
                return "first"
            second_next_started.set()
            release_second_next.wait()
            raise StopIteration

        def close(self):
            close_called.set()

    stream = coordinator.stream(
        "call_assistant",
        "call_suggestion",
        _snapshot(feature="call_assistant"),
        BlockingAfterFirst,
    )
    assert next(stream) == "first"
    assert second_next_started.wait(1)

    try:
        stream.close()
        assert coordinator.live_state().running == 1
        _wait_until(
            lambda: _metric_count(
                factory, result_status="cancelled"
            )
            == 1
        )
        assert _metric_count(factory, result_status="cancelled") == 1
    finally:
        release_second_next.set()
        safety.cancel()

    _wait_until(close_called.is_set)
    _wait_until(lambda: coordinator.live_state().running == 0)
    assert coordinator.shutdown(timeout_seconds=0.1) is True
    assert _metric_count(factory) == 1


def test_stream_success_and_provider_error_both_release_gate(metric_store):
    factory, _ = metric_store
    coordinator = InferenceCoordinator(factory)

    assert list(
        coordinator.stream(
            "call_assistant",
            "call_suggestion",
            _snapshot(feature="call_assistant"),
            lambda: iter(("one", "two")),
        )
    ) == ["one", "two"]

    upstream_closed = Event()

    def broken_source():
        try:
            yield "first"
            raise RuntimeError("upstream failed")
        finally:
            upstream_closed.set()

    stream = coordinator.stream(
        "call_assistant",
        "call_suggestion",
        _snapshot(feature="call_assistant"),
        broken_source,
    )
    assert next(stream) == "first"
    with pytest.raises(RuntimeError, match="upstream failed"):
        next(stream)

    assert upstream_closed.is_set()
    _wait_until(lambda: coordinator.live_state().running == 0)
    assert coordinator.live_state().running == 0
    assert coordinator.live_state().queued == 0
    _wait_until(lambda: _metric_count(factory) == 2)
    with factory() as session:
        statuses = sorted(
            metric.result_status
            for metric in session.scalars(select(ModelRequestMetric))
        )
        assert statuses == ["error", "success"]


def test_worker_error_cannot_be_relabelled_as_consumer_cancellation(
    metric_store, monkeypatch
):
    factory, _ = metric_store
    coordinator = InferenceCoordinator(factory)
    terminal_published = Event()
    release_worker = Event()
    original_finish = _StreamState.finish

    def blocked_finish(state, **kwargs):
        original_finish(state, **kwargs)
        terminal_published.set()
        assert release_worker.wait(1)

    monkeypatch.setattr(_StreamState, "finish", blocked_finish)

    def broken_source():
        raise RuntimeError("terminal provider error")
        yield

    stream = coordinator.stream(
        "call_assistant",
        "call_suggestion",
        _snapshot(feature="call_assistant"),
        broken_source,
    )
    try:
        with pytest.raises(RuntimeError, match="terminal provider"):
            next(stream)
        assert terminal_published.is_set()
    finally:
        release_worker.set()

    _wait_until(lambda: coordinator.live_state().running == 0)
    _wait_until(lambda: _metric_count(factory) == 1)
    with factory() as session:
        metric = session.scalar(select(ModelRequestMetric))
        assert metric is not None
        assert metric.result_status == "error"


def test_stream_timeout_closes_upstream_and_releases_gate(metric_store):
    factory, _ = metric_store
    now = [20.0]
    coordinator = InferenceCoordinator(
        factory,
        timeout_seconds=0.1,
        clock=lambda: now[0],
    )
    upstream_closed = Event()

    def source():
        try:
            now[0] = 20.2
            yield "late"
        finally:
            upstream_closed.set()

    with pytest.raises(InferenceTimedOut):
        next(
            coordinator.stream(
                "call_assistant",
                "call_suggestion",
                _snapshot(feature="call_assistant"),
                source,
            )
        )

    assert upstream_closed.is_set()
    _wait_until(lambda: coordinator.live_state().running == 0)
    assert coordinator.live_state().running == 0
    assert coordinator.live_state().queued == 0
    _wait_until(lambda: _metric_count(factory) == 1)
    with factory() as session:
        metric = session.scalar(select(ModelRequestMetric))
        assert metric is not None
        assert metric.result_status == "timeout"


def test_stream_that_finishes_after_deadline_still_times_out(metric_store):
    factory, _ = metric_store
    now = [50.0]
    coordinator = InferenceCoordinator(
        factory,
        timeout_seconds=0.1,
        clock=lambda: now[0],
    )

    def late_empty_iterator():
        now[0] = 50.2
        return iter(())

    with pytest.raises(InferenceTimedOut):
        next(
            coordinator.stream(
                "call_assistant",
                "call_suggestion",
                _snapshot(feature="call_assistant"),
                late_empty_iterator,
            )
        )

    _wait_until(lambda: coordinator.live_state().running == 0)
    _wait_until(
        lambda: _metric_count(factory, result_status="timeout") == 1
    )


def test_blocking_stream_next_times_out_and_close_error_cannot_leak_slot(
    metric_store,
):
    factory, _ = metric_store
    coordinator = InferenceCoordinator(factory, timeout_seconds=0.03)
    next_started = Event()
    release_next = Event()
    close_called = Event()
    safety = Timer(0.3, release_next.set)
    safety.start()

    class BlockingIterator:
        def __iter__(self):
            return self

        def __next__(self):
            next_started.set()
            release_next.wait()
            return "late"

        def close(self):
            close_called.set()
            raise CancelledError()

    stream = coordinator.stream(
        "call_assistant",
        "call_suggestion",
        _snapshot(feature="call_assistant"),
        BlockingIterator,
    )
    started_at = monotonic()
    try:
        with pytest.raises(InferenceTimedOut):
            next(stream)
        elapsed = monotonic() - started_at

        assert elapsed < 0.15
        assert next_started.is_set()
        assert coordinator.live_state().running == 1
        _wait_until(
            lambda: _metric_count(
                factory, result_status="timeout"
            )
            == 1
        )
        assert _metric_count(factory, result_status="timeout") == 1
    finally:
        release_next.set()
        safety.cancel()

    _wait_until(close_called.is_set)
    _wait_until(lambda: coordinator.live_state().running == 0)
    assert coordinator.live_state().queued == 0
    _wait_until(
        lambda: _metric_count(factory, result_status="timeout") == 1
    )
    assert coordinator.shutdown(timeout_seconds=0.1) is True
    assert _metric_count(factory) == 1


def test_mock_records_metrics_without_consuming_local_gate(metric_store):
    factory, _ = metric_store
    coordinator = InferenceCoordinator(factory)
    observed = []

    result = coordinator.execute(
        "knowledge",
        "knowledge_answer",
        _snapshot("mock"),
        lambda: observed.append(coordinator.live_state()) or "mock answer",
    )

    assert result == "mock answer"
    assert observed[0].running == 0
    assert observed[0].queued == 0
    _wait_until(lambda: _metric_count(factory) == 1)
    with factory() as session:
        metric = session.scalar(select(ModelRequestMetric))
        assert metric is not None
        assert metric.mode == "mock"
        assert metric.model == "mock"
        assert metric.input_tokens is None
        assert metric.output_tokens is None


def test_mock_stream_is_inline_and_never_constructs_worker(
    metric_store, monkeypatch
):
    factory, _ = metric_store
    coordinator = InferenceCoordinator(factory)

    def forbidden_thread(**_):
        raise AssertionError("Mock stream must remain inline")

    monkeypatch.setattr(inference_module, "Thread", forbidden_thread)

    assert list(
        coordinator.stream(
            "call_assistant",
            "call_suggestion",
            _snapshot("mock", feature="call_assistant"),
            lambda: iter(("one", "two")),
        )
    ) == ["one", "two"]
    assert coordinator.live_state().running == 0
    assert coordinator.live_state().queued == 0
    _wait_until(
        lambda: _metric_count(factory, result_status="success") == 1
    )
    assert _metric_count(factory, result_status="success") == 1


def test_shutdown_cancels_stream_waits_for_worker_and_rejects_new_work(
    metric_store,
):
    factory, _ = metric_store
    coordinator = InferenceCoordinator(factory)
    upstream_closed = Event()

    def source():
        try:
            yield "first"
            yield "second"
        finally:
            upstream_closed.set()

    stream = coordinator.stream(
        "call_assistant",
        "call_suggestion",
        _snapshot(feature="call_assistant"),
        source,
    )
    assert next(stream) == "first"
    assert coordinator.live_state().running == 1

    assert coordinator.shutdown(timeout_seconds=0.2) is False

    assert upstream_closed.is_set()
    assert coordinator.live_state().running == 0
    assert coordinator.live_state().queued == 0
    assert _metric_count(factory, result_status="cancelled") == 1
    with pytest.raises(RuntimeError, match="shut"):
        coordinator.execute(
            "knowledge",
            "knowledge_answer",
            _snapshot(),
            lambda: None,
        )
    with pytest.raises(RuntimeError, match="shut"):
        coordinator.stream(
            "call_assistant",
            "call_suggestion",
            _snapshot(feature="call_assistant"),
            lambda: iter(()),
        )
    stream.close()
    assert coordinator.shutdown(timeout_seconds=0.1) is True
    assert _metric_count(factory) == 1


def test_shutdown_is_bounded_and_late_worker_cannot_touch_database(
    metric_store,
):
    factory, _ = metric_store
    callback_started = Event()
    release_callback = Event()
    late_database_use = Event()
    persistence_disabled = Event()
    result = []
    errors = []

    def guarded_factory():
        if persistence_disabled.is_set():
            late_database_use.set()
            raise AssertionError("late persistence after shutdown")
        return factory()

    coordinator = InferenceCoordinator(
        guarded_factory, timeout_seconds=5
    )

    def block():
        callback_started.set()
        release_callback.wait()
        return "finished"

    def run():
        try:
            result.append(
                coordinator.execute(
                    "knowledge",
                    "knowledge_answer",
                    _snapshot(),
                    block,
                )
            )
        except BaseException as exc:
            errors.append(exc)

    caller = Thread(target=run)
    caller.start()
    assert callback_started.wait(1)

    started_at = monotonic()
    assert coordinator.shutdown(timeout_seconds=0.03) is False
    elapsed = monotonic() - started_at
    persistence_disabled.set()

    assert elapsed < 0.15
    assert coordinator.live_state().running == 1
    release_callback.set()
    caller.join(timeout=1)
    assert not caller.is_alive()
    _wait_until(lambda: coordinator.live_state().running == 0)

    assert result == ["finished"]
    assert errors == []
    assert not late_database_use.is_set()
    assert _metric_count(factory) == 0


def test_app_shutdown_stops_inference_before_database_and_provider(
    tmp_path, monkeypatch
):
    events = []
    original_dispose = Engine.dispose
    original_close = OllamaClient.close

    def record_shutdown(self, timeout_seconds=1.0):
        events.append("inference")
        return True

    def record_dispose(self, *args, **kwargs):
        events.append("database")
        return original_dispose(self, *args, **kwargs)

    def record_close(self):
        events.append("provider")
        return original_close(self)

    monkeypatch.setattr(
        InferenceCoordinator, "shutdown", record_shutdown
    )
    monkeypatch.setattr(Engine, "dispose", record_dispose)
    monkeypatch.setattr(OllamaClient, "close", record_close)

    settings = Settings(runtime_dir=tmp_path)
    with TestClient(create_app(settings)):
        pass

    assert "inference" in events
    assert events.index("inference") < events.index("database")
    assert events.index("inference") < events.index("provider")


def test_metric_row_and_database_bytes_never_contain_business_content_or_secrets(
    metric_store,
):
    factory, database_path = metric_store
    coordinator = InferenceCoordinator(factory)
    question = "SENTINEL_QUESTION_7b8a"
    answer = "SENTINEL_ANSWER_9c2f"
    document = "SENTINEL_DOCUMENT_4d1e"
    api_key = "SENTINEL_API_KEY_6a3d"
    snapshot = _snapshot()
    snapshot.provider._api_key = api_key

    assert coordinator.execute(
        "knowledge",
        "knowledge_answer",
        snapshot,
        lambda: {
            "question": question,
            "answer": answer,
            "document": document,
        },
    )["answer"] == answer

    _wait_until(lambda: _metric_count(factory) == 1)
    with factory() as session:
        metric = session.scalar(select(ModelRequestMetric))
        assert metric is not None
        serialized = repr(
            {
                column.name: getattr(metric, column.name)
                for column in ModelRequestMetric.__table__.columns
            }
        )
    raw_database = database_path.read_bytes()
    for secret in (question, answer, document, api_key):
        assert secret not in serialized
        assert secret.encode() not in raw_database


def test_metric_dimensions_reject_content_labels_and_bound_model_cardinality(
    metric_store,
):
    factory, database_path = metric_store
    coordinator = InferenceCoordinator(factory)
    secret_feature = "SENTINEL_FEATURE_CONTENT_8f2a"
    secret_operation = "SENTINEL_OPERATION_CONTENT_3d6b"
    secret_model = "SENTINEL_MODEL_CONTENT_5c1e"
    secret_mode = "SENTINEL_MODE_CONTENT_9a4f"
    snapshot = _snapshot(model=secret_model)
    invalid_mode_snapshot = FeatureProviderSnapshot(
        feature="knowledge",
        mode=secret_mode,
        model=None,
        provider=MockProvider(delay_ms=0),
    )

    with pytest.raises(ValueError, match="feature"):
        coordinator.execute(
            secret_feature,
            "knowledge_answer",
            snapshot,
            lambda: None,
        )
    with pytest.raises(ValueError, match="operation"):
        coordinator.execute(
            "knowledge",
            secret_operation,
            snapshot,
            lambda: None,
        )
    with pytest.raises(ValueError, match="mode"):
        coordinator.execute(
            "knowledge",
            "knowledge_answer",
            invalid_mode_snapshot,
            lambda: None,
        )

    assert coordinator.execute(
        "knowledge",
        "knowledge_answer",
        snapshot,
        lambda: "ok",
    ) == "ok"
    _wait_until(lambda: _metric_count(factory) == 1)

    with factory() as session:
        metric = session.scalar(select(ModelRequestMetric))
        assert metric is not None
        assert metric.feature == "knowledge"
        assert metric.operation == "knowledge_answer"
        assert metric.model == "local"
    raw_database = database_path.read_bytes()
    for secret in (
        secret_feature,
        secret_operation,
        secret_model,
        secret_mode,
    ):
        assert secret.encode() not in raw_database


def test_approved_qwen_model_name_is_preserved_in_metric(metric_store):
    factory, _ = metric_store
    coordinator = InferenceCoordinator(factory)

    assert coordinator.execute(
        "knowledge",
        "knowledge_answer",
        _snapshot(model="qwen3:1.7b-q4_K_M"),
        lambda: "ok",
    ) == "ok"
    _wait_until(lambda: _metric_count(factory) == 1)

    with factory() as session:
        metric = session.scalar(select(ModelRequestMetric))
        assert metric is not None
        assert metric.model == "qwen3:1.7b-q4_K_M"


def test_metric_write_failure_does_not_mask_business_result():
    def broken_session_factory():
        raise RuntimeError("metrics unavailable")

    coordinator = InferenceCoordinator(broken_session_factory)

    assert (
        coordinator.execute(
            "knowledge",
            "knowledge_answer",
            _snapshot("mock"),
            lambda: "business result",
        )
        == "business result"
    )


def test_blocked_metric_write_never_extends_hard_caller_timeout(
    monkeypatch,
):
    from app.services.inference_metrics import _InferenceTelemetry

    write_started = Event()
    release_write = Event()
    callback_started = Event()
    release_callback = Event()
    safety = Timer(
        0.3,
        lambda: (release_write.set(), release_callback.set()),
    )
    safety.start()

    def blocked_persist(_metric):
        write_started.set()
        release_write.wait()

    telemetry = _InferenceTelemetry(
        blocked_persist,
        close=lambda: None,
    )
    monkeypatch.setattr(
        inference_module,
        "_create_inference_telemetry",
        lambda *_args, **_kwargs: telemetry,
    )
    coordinator = InferenceCoordinator(
        lambda: (_ for _ in ()).throw(
            AssertionError("application factory must remain unused")
        ),
        timeout_seconds=0.03,
    )

    def blocked_callback():
        callback_started.set()
        release_callback.wait()

    started_at = monotonic()
    try:
        with pytest.raises(InferenceTimedOut):
            coordinator.execute(
                "knowledge",
                "knowledge_answer",
                _snapshot(),
                blocked_callback,
            )
        elapsed = monotonic() - started_at

        assert callback_started.is_set()
        assert write_started.wait(0.1)
        assert elapsed < 0.15
    finally:
        release_write.set()
        release_callback.set()
        safety.cancel()
        coordinator.shutdown(timeout_seconds=0.2)


def _telemetry_event(request_id: str):
    from app.services.inference_metrics import _InferenceMetricEvent

    return _InferenceMetricEvent(
        feature="knowledge",
        mode="local",
        model="local",
        request_id=request_id,
        operation="knowledge_answer",
        queue_wait_ms=0,
        ttft_ms=None,
        elapsed_ms=1,
        input_tokens=None,
        output_tokens=None,
        result_status="success",
    )


def test_telemetry_submission_is_nonblocking_and_drops_full_or_closed():
    from app.services.inference_metrics import _InferenceTelemetry

    write_started = Event()
    release_write = Event()
    persisted = []

    def persist(metric):
        write_started.set()
        release_write.wait()
        persisted.append(metric.request_id)

    telemetry = _InferenceTelemetry(
        persist,
        close=lambda: None,
        capacity=1,
    )
    assert telemetry.submit(_telemetry_event("in-flight"))
    assert write_started.wait(1)
    assert telemetry.submit(_telemetry_event("queued"))

    started_at = monotonic()
    assert not telemetry.submit(_telemetry_event("dropped"))
    assert monotonic() - started_at < 0.05

    release_write.set()
    assert telemetry.shutdown(timeout_seconds=0.5)
    assert persisted == ["in-flight", "queued"]
    assert not telemetry.submit(_telemetry_event("closed"))


def test_telemetry_shutdown_rejects_new_events_and_drains_accounted_work():
    from app.services.inference_metrics import _InferenceTelemetry

    write_started = Event()
    release_write = Event()
    persisted = []
    shutdown_result = []

    def persist(metric):
        write_started.set()
        release_write.wait()
        persisted.append(metric.request_id)

    telemetry = _InferenceTelemetry(
        persist,
        close=lambda: None,
        capacity=2,
    )
    assert telemetry.submit(_telemetry_event("in-flight"))
    assert write_started.wait(1)
    assert telemetry.submit(_telemetry_event("queued"))

    shutdown = Thread(
        target=lambda: shutdown_result.append(
            telemetry.shutdown(timeout_seconds=0.5)
        )
    )
    shutdown.start()
    _wait_until(lambda: not telemetry.state().accepting)

    state = telemetry.state()
    assert state.in_flight == 1
    assert state.queued == 1
    assert not state.terminal
    assert not telemetry.submit(_telemetry_event("after-close"))
    shutdown.join(timeout=0.03)
    assert shutdown.is_alive()

    release_write.set()
    shutdown.join(timeout=1)
    assert not shutdown.is_alive()
    assert shutdown_result == [True]
    assert persisted == ["in-flight", "queued"]
    assert telemetry.state().queued == 0
    assert telemetry.state().in_flight == 0
    assert telemetry.state().terminal


def test_telemetry_writer_construction_failure_closes_and_drops(
    monkeypatch,
):
    import app.services.inference_metrics as metrics_module
    from app.services.inference_metrics import _InferenceTelemetry

    closed = Event()

    def fail_thread_construction(**_):
        raise MemoryError("telemetry thread unavailable")

    monkeypatch.setattr(
        metrics_module,
        "Thread",
        fail_thread_construction,
    )
    telemetry = _InferenceTelemetry(
        lambda _: None,
        close=closed.set,
    )

    assert closed.is_set()
    assert not telemetry.state().accepting
    assert telemetry.state().terminal
    assert not telemetry.submit(_telemetry_event("dropped"))
    assert telemetry.shutdown(timeout_seconds=0.01)


def test_late_telemetry_writer_owns_engine_after_main_engine_disposal(tmp_path):
    from app.services.inference_metrics import (
        _InferenceTelemetry,
        _SqlAlchemyMetricWriter,
    )

    database_url = f"sqlite:///{tmp_path / 'writer-ownership.db'}"
    main_engine = create_engine(
        database_url,
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(main_engine)
    writer = _SqlAlchemyMetricWriter(database_url)
    telemetry = _InferenceTelemetry(
        writer.persist,
        close=writer.close,
    )
    insert_started = Event()
    release_insert = Event()

    @event.listens_for(writer.engine, "before_cursor_execute")
    def block_metric_insert(
        _connection,
        _cursor,
        statement,
        _parameters,
        _context,
        _executemany,
    ):
        if statement.lstrip().upper().startswith(
            "INSERT INTO MODEL_REQUEST_METRICS"
        ):
            insert_started.set()
            release_insert.wait()

    assert writer.engine is not main_engine
    assert telemetry.submit(_telemetry_event("late-writer"))
    assert insert_started.wait(1)
    assert not telemetry.shutdown(timeout_seconds=0.01)
    assert not writer.closed

    main_engine.dispose()
    release_insert.set()

    assert telemetry.shutdown(timeout_seconds=0.5)
    assert writer.closed
    verification_engine = create_engine(database_url)
    try:
        with Session(verification_engine) as session:
            metric = session.scalar(
                select(ModelRequestMetric).where(
                    ModelRequestMetric.request_id == "late-writer"
                )
            )
            assert metric is not None
    finally:
        verification_engine.dispose()


def test_local_lifecycle_close_wins_before_spawn_and_prevents_provider():
    from app.services.inference_lifecycle import _LocalWorkerLifecycle

    lifecycle = _LocalWorkerLifecycle()
    provider_started = Event()
    admission_released = Event()
    request = lifecycle.register_request()

    close = Thread(target=lifecycle.begin_shutdown)
    close.start()
    close.join(timeout=1)
    assert not close.is_alive()
    assert lifecycle.closing

    request.release_once()
    with pytest.raises(RuntimeError, match="shut"):
        lifecycle.spawn(
            target=provider_started.set,
            name="must-not-start",
            release_admission=admission_released.set,
            cancel_on_shutdown=None,
        )

    assert admission_released.is_set()
    assert not provider_started.is_set()
    assert lifecycle.wait_for_quiescence(timeout_seconds=0.1)


def test_local_lifecycle_unregisters_when_post_release_cleanup_raises():
    from app.services.inference_lifecycle import _LocalWorkerLifecycle

    lifecycle = _LocalWorkerLifecycle()
    admission_released = Event()
    errors = []

    def fail_cleanup():
        raise MemoryError("post-release cleanup failed")

    def capturing_thread_factory(*, target, name, daemon):
        return Thread(
            target=lambda: _capture_error(errors, target),
            name=name,
            daemon=daemon,
        )

    worker = lifecycle.spawn(
        target=lambda: None,
        name="cleanup-failure",
        release_admission=admission_released.set,
        cancel_on_shutdown=None,
        after_release=fail_cleanup,
        thread_factory=capturing_thread_factory,
    )
    worker.join(timeout=1)

    assert not worker.is_alive()
    assert admission_released.is_set()
    assert len(errors) == 1
    assert isinstance(errors[0], MemoryError)
    assert lifecycle.wait_for_quiescence(timeout_seconds=0.1)


def test_shutdown_waits_for_queued_caller_lifecycle(metric_store, monkeypatch):
    factory, _ = metric_store
    coordinator = InferenceCoordinator(factory, timeout_seconds=2)
    owner_started = Event()
    release_owner = Event()
    queued_woken = Event()
    release_queued_cleanup = Event()
    owner_errors = []
    queued_errors = []
    shutdown_result = []
    original_acquire = coordinator._acquire

    def pause_woken_queued_caller(started_at, deadline):
        try:
            return original_acquire(started_at, deadline)
        except RuntimeError:
            queued_woken.set()
            release_queued_cleanup.wait()
            raise

    monkeypatch.setattr(
        coordinator,
        "_acquire",
        pause_woken_queued_caller,
    )

    def hold_owner():
        owner_started.set()
        release_owner.wait()

    owner = Thread(
        target=lambda: _capture_error(
            owner_errors,
            lambda: coordinator.execute(
                "knowledge",
                "knowledge_answer",
                _snapshot(),
                hold_owner,
            ),
        )
    )
    queued = Thread(
        target=lambda: _capture_error(
            queued_errors,
            lambda: coordinator.execute(
                "knowledge",
                "knowledge_answer",
                _snapshot(),
                lambda: None,
            ),
        )
    )
    owner.start()
    assert owner_started.wait(1)
    queued.start()
    _wait_until(lambda: coordinator.live_state().queued == 1)

    shutdown = Thread(
        target=lambda: shutdown_result.append(
            coordinator.shutdown(timeout_seconds=0.5)
        )
    )
    shutdown.start()
    assert queued_woken.wait(1)
    release_owner.set()
    owner.join(timeout=1)
    assert not owner.is_alive()

    shutdown.join(timeout=0.03)
    try:
        assert shutdown.is_alive()
    finally:
        release_queued_cleanup.set()

    queued.join(timeout=1)
    shutdown.join(timeout=1)
    assert not queued.is_alive()
    assert not shutdown.is_alive()
    assert shutdown_result == [True]
    assert owner_errors == []
    assert len(queued_errors) == 1
    assert isinstance(queued_errors[0], RuntimeError)


def test_telemetry_without_url_is_closed_and_never_invokes_factory():
    from app.services.inference_metrics import (
        _create_inference_telemetry,
    )

    factory_invoked = Event()

    def forbidden_application_factory():
        factory_invoked.set()
        raise AssertionError(
            "telemetry used the application session factory"
        )

    telemetry = _create_inference_telemetry(
        forbidden_application_factory,
        database_url=None,
    )
    try:
        started_at = monotonic()
        assert not telemetry.submit(
            _telemetry_event("drop-without-url")
        )
        assert monotonic() - started_at < 0.05
        assert not factory_invoked.is_set()
        assert not telemetry.state().accepting
        assert telemetry.state().terminal
        assert telemetry.shutdown(timeout_seconds=0.01)
    finally:
        telemetry.shutdown(timeout_seconds=0.1)


@pytest.mark.parametrize(
    ("mode", "streaming"),
    (
        ("local", False),
        ("mock", False),
        ("local", True),
        ("mock", True),
    ),
)
def test_clock_failure_releases_complete_request_lifecycle(
    metric_store,
    mode,
    streaming,
):
    factory, _ = metric_store

    def broken_clock():
        raise RuntimeError("clock unavailable")

    coordinator = InferenceCoordinator(factory, clock=broken_clock)
    snapshot = _snapshot(
        mode,
        feature="call_assistant" if streaming else "knowledge",
    )

    with pytest.raises(RuntimeError, match="clock"):
        if streaming:
            next(
                coordinator.stream(
                    "call_assistant",
                    "call_suggestion",
                    snapshot,
                    lambda: iter(("never",)),
                )
            )
        else:
            coordinator.execute(
                "knowledge",
                "knowledge_answer",
                snapshot,
                lambda: None,
            )

    assert coordinator.shutdown(timeout_seconds=0.1)


def test_request_registration_context_releases_on_base_exception():
    from app.services.inference_lifecycle import _LocalWorkerLifecycle

    lifecycle = _LocalWorkerLifecycle()

    with pytest.raises(MemoryError, match="logical request"):
        with lifecycle.register_request():
            raise MemoryError("logical request failed")

    lifecycle.begin_shutdown()
    assert lifecycle.wait_for_quiescence(timeout_seconds=0.1)


def test_shutdown_waits_for_execute_caller_terminal_before_telemetry_close(
    metric_store,
    monkeypatch,
):
    factory, _ = metric_store
    now = [50.0]
    coordinator = InferenceCoordinator(
        factory,
        timeout_seconds=0.1,
        clock=lambda: now[0],
    )
    caller_terminal_started = Event()
    release_caller_terminal = Event()
    caller_errors = []
    shutdown_result = []
    original_result = coordinator._execution_result

    def pause_caller_terminal(state, context):
        caller_terminal_started.set()
        release_caller_terminal.wait()
        return original_result(state, context)

    monkeypatch.setattr(
        coordinator,
        "_execution_result",
        pause_caller_terminal,
    )

    def finish_after_deadline():
        now[0] = 50.2
        return "late"

    caller = Thread(
        target=lambda: _capture_error(
            caller_errors,
            lambda: coordinator.execute(
                "knowledge",
                "knowledge_answer",
                _snapshot(),
                finish_after_deadline,
            ),
        )
    )
    caller.start()
    assert caller_terminal_started.wait(1)

    shutdown = Thread(
        target=lambda: shutdown_result.append(
            coordinator.shutdown(timeout_seconds=0.5)
        )
    )
    shutdown.start()
    shutdown.join(timeout=0.03)
    try:
        assert shutdown.is_alive()
    finally:
        release_caller_terminal.set()

    caller.join(timeout=1)
    shutdown.join(timeout=1)
    assert not caller.is_alive()
    assert not shutdown.is_alive()
    assert len(caller_errors) == 1
    assert isinstance(caller_errors[0], InferenceTimedOut)
    assert shutdown_result == [True]
    assert _metric_count(factory, result_status="timeout") == 1


def test_shutdown_waits_for_complete_local_stream_consumer_lifecycle(
    metric_store,
):
    factory, _ = metric_store
    coordinator = InferenceCoordinator(factory)
    shutdown_result = []
    stream = coordinator.stream(
        "call_assistant",
        "call_suggestion",
        _snapshot(feature="call_assistant"),
        lambda: iter(("one",)),
    )

    assert next(stream) == "one"
    _wait_until(lambda: coordinator.live_state().running == 0)

    shutdown = Thread(
        target=lambda: shutdown_result.append(
            coordinator.shutdown(timeout_seconds=0.5)
        )
    )
    shutdown.start()
    shutdown.join(timeout=0.03)
    try:
        assert shutdown.is_alive()
    finally:
        stream.close()

    shutdown.join(timeout=1)
    assert not shutdown.is_alive()
    assert shutdown_result == [True]
    assert _metric_count(factory, result_status="success") == 1
