from asyncio import CancelledError as AsyncCancelledError
from collections.abc import Callable, Iterator
from concurrent.futures import CancelledError as FutureCancelledError
from dataclasses import dataclass, field
from threading import (
    Condition,
    Event,
    Lock,
    Thread,
)
from time import monotonic
from typing import Generic, TypeVar, cast
from uuid import uuid4

from app.providers.base import ProviderUsage
from app.providers.feature_router import FeatureProviderSnapshot
from app.services.inference_lifecycle import _LocalWorkerLifecycle
from app.services.inference_metrics import (
    InferenceStats as InferenceStats,
    _InferenceMetricRecorder,
    _create_inference_telemetry,
)


Result = TypeVar("Result")
Item = TypeVar("Item")
_MISSING = object()
_NULL_USAGE = ProviderUsage(input_tokens=None, output_tokens=None)
_CANCELLATION_ERRORS = (
    AsyncCancelledError,
    FutureCancelledError,
    GeneratorExit,
)


class InferenceQueueFull(RuntimeError):
    """Raised before admission when all local waiting positions are occupied."""


class InferenceTimedOut(RuntimeError):
    """Raised when the queue-plus-execution deadline is exhausted."""


class InferenceCancelled(RuntimeError):
    """Raised when request cancellation wins before provider work."""


class InferenceCancellation:
    """Linearizes cancellation against local stream admission."""

    def __init__(self):
        self._lock = Lock()
        self._cancelled = False
        self._admission_claimed = False
        self._provider_started = False
        self._callbacks: set[Callable[[], None]] = set()

    def cancel(self) -> bool:
        with self._lock:
            if self._cancelled:
                return False
            self._cancelled = True
            callbacks = tuple(self._callbacks)
        for callback in callbacks:
            callback()
        return True

    def is_cancelled(self) -> bool:
        with self._lock:
            return self._cancelled

    def admission_won(self) -> bool:
        with self._lock:
            return self._admission_claimed

    def claim_admission(self) -> bool:
        with self._lock:
            if self._cancelled and not self._admission_claimed:
                return False
            self._admission_claimed = True
            return True

    def claim_provider_start(self) -> bool:
        with self._lock:
            if self._cancelled and not self._provider_started:
                return False
            self._provider_started = True
            return True

    def provider_start_won(self) -> bool:
        with self._lock:
            return self._provider_started

    def register_wakeup(
        self, callback: Callable[[], None]
    ) -> Callable[[], None]:
        invoke = False
        with self._lock:
            self._callbacks.add(callback)
            invoke = self._cancelled
        if invoke:
            callback()

        def unregister() -> None:
            with self._lock:
                self._callbacks.discard(callback)

        return unregister


@dataclass(frozen=True)
class InferenceLiveState:
    running: int
    queued: int
    concurrency: int
    max_queue: int


class _AdmissionLease:
    """Transfers one admitted slot safely across caller and worker."""

    def __init__(self, release: Callable[[], None]):
        self._release = release
        self._lock = Lock()
        self._released = False

    def release_once(self) -> bool:
        with self._lock:
            if self._released:
                return False
            self._released = True
        self._release()
        return True


class _TerminalClaim:
    """Allows exactly one caller/worker terminal metric publication."""

    def __init__(self):
        self._lock = Lock()
        self._claimed = False

    def claim(self) -> bool:
        with self._lock:
            if self._claimed:
                return False
            self._claimed = True
            return True


@dataclass(frozen=True)
class _RequestContext:
    request_id: str
    operation: str
    snapshot: FeatureProviderSnapshot
    started_at: float
    deadline: float
    queue_wait_ms: int
    terminal: _TerminalClaim = field(default_factory=_TerminalClaim)


@dataclass
class _ExecutionState(Generic[Result]):
    done: Event = field(default_factory=Event)
    lock: Lock = field(default_factory=Lock)
    result: object = _MISSING
    error: BaseException | None = None
    usage: ProviderUsage = _NULL_USAGE
    completed_at: float | None = None
    base_status: str = "error"
    override_status: str | None = None

    def should_start(self, now: float, deadline: float) -> bool:
        with self.lock:
            return self.override_status is None and now < deadline

    def override(self, status: str, deadline: float) -> bool:
        with self.lock:
            if (
                self.completed_at is not None
                and self.completed_at < deadline
            ):
                return False
            if self.override_status is None:
                self.override_status = status
            return True

    def status(self, deadline: float) -> str:
        with self.lock:
            if self.override_status is not None:
                return self.override_status
            if (
                self.completed_at is not None
                and self.completed_at >= deadline
            ):
                return "timeout"
            return self.base_status


@dataclass
class _StreamState(Generic[Item]):
    condition: Condition = field(default_factory=Condition)
    pending: object = _MISSING
    finished: bool = False
    cancelled: bool = False
    error: BaseException | None = None
    usage: ProviderUsage = _NULL_USAGE
    completed_at: float | None = None
    first_item_at: float | None = None
    base_status: str = "error"
    override_status: str | None = None

    def publish(self, item: Item, observed_at: float) -> bool:
        with self.condition:
            while self.pending is not _MISSING and not self.cancelled:
                self.condition.wait()
            if self.cancelled:
                return False
            if self.first_item_at is None:
                self.first_item_at = observed_at
            self.pending = item
            self.condition.notify_all()
            return True

    def wait_until_drained(self) -> None:
        with self.condition:
            while self.pending is not _MISSING and not self.cancelled:
                self.condition.wait()

    def cancel(self, status: str) -> bool:
        with self.condition:
            if self.finished:
                return False
            if self.override_status is None:
                self.override_status = status
            self.cancelled = True
            self.pending = _MISSING
            self.condition.notify_all()
            return True

    def is_cancelled(self) -> bool:
        with self.condition:
            return self.cancelled

    def finish(
        self,
        *,
        error: BaseException | None,
        usage: ProviderUsage,
        completed_at: float,
        base_status: str,
    ) -> None:
        with self.condition:
            self.error = error
            self.usage = usage
            self.completed_at = completed_at
            self.base_status = base_status
            self.finished = True
            self.condition.notify_all()

    def receive(
        self,
        *,
        deadline: float,
        clock: Callable[[], float],
    ) -> Item:
        with self.condition:
            while True:
                if self.cancelled and not self.finished:
                    raise FutureCancelledError()
                now = clock()
                completed_in_time = (
                    self.finished
                    and self.completed_at is not None
                    and self.completed_at < deadline
                )
                if now >= deadline and not completed_in_time:
                    if self.override_status is None:
                        self.override_status = "timeout"
                    self.cancelled = True
                    self.pending = _MISSING
                    self.condition.notify_all()
                    raise InferenceTimedOut(
                        "inference deadline exhausted"
                    )
                if self.pending is not _MISSING:
                    item = self.pending
                    self.pending = _MISSING
                    self.condition.notify_all()
                    return cast(Item, item)
                if self.finished:
                    if self.error is not None:
                        raise self.error
                    raise StopIteration
                self.condition.wait(deadline - now)

    def status(self, deadline: float) -> str:
        with self.condition:
            if self.override_status is not None:
                return self.override_status
            if (
                self.completed_at is not None
                and self.completed_at >= deadline
            ):
                return "timeout"
            return self.base_status

    def ttft_ms(self, started_at: float) -> int | None:
        with self.condition:
            if self.first_item_at is None:
                return None
            return max(
                0, int((self.first_item_at - started_at) * 1000)
            )


@dataclass
class _StreamWorkerOutcome:
    error: BaseException | None = None
    usage: ProviderUsage = _NULL_USAGE
    completed_at: float = 0.0
    base_status: str = "error"


class InferenceCoordinator:
    """Bounds local inference and owns worker/admission lifecycles."""

    def __init__(
        self,
        session_factory: Callable[[], object],
        concurrency: int = 1,
        max_queue: int = 3,
        timeout_seconds: float = 90.0,
        clock: Callable[[], float] = monotonic,
        telemetry_database_url: str | None = None,
    ):
        if concurrency <= 0:
            raise ValueError("concurrency must be greater than zero")
        if max_queue < 0:
            raise ValueError("max_queue must not be negative")
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be greater than zero")
        self._concurrency = concurrency
        self._max_queue = max_queue
        self._timeout_seconds = timeout_seconds
        self._clock = clock
        self._condition = Condition()
        self._running = 0
        self._queued = 0
        self._lifecycle = _LocalWorkerLifecycle()
        self._metrics = _InferenceMetricRecorder()
        self._telemetry = _create_inference_telemetry(
            session_factory,
            telemetry_database_url,
        )

    def live_state(self) -> InferenceLiveState:
        with self._condition:
            return InferenceLiveState(
                running=self._running,
                queued=self._queued,
                concurrency=self._concurrency,
                max_queue=self._max_queue,
            )

    def execute(
        self,
        feature: str,
        operation: str,
        snapshot: FeatureProviderSnapshot,
        callback: Callable[[], Result],
    ) -> Result:
        self._metrics.validate_dimensions(
            feature, operation, snapshot
        )
        with self._lifecycle.register_request():
            return self._execute_registered(
                operation,
                snapshot,
                callback,
            )

    def _execute_registered(
        self,
        operation: str,
        snapshot: FeatureProviderSnapshot,
        callback: Callable[[], Result],
    ) -> Result:
        started_at = self._clock()
        context = _RequestContext(
            request_id=str(uuid4()),
            operation=operation,
            snapshot=snapshot,
            started_at=started_at,
            deadline=started_at + self._timeout_seconds,
            queue_wait_ms=0,
        )
        if snapshot.mode != "local":
            return self._execute_mock(context, callback)

        try:
            queue_wait_ms = self._acquire(
                context.started_at, context.deadline
            )
            context = self._with_queue_wait(
                context, queue_wait_ms
            )
        except InferenceQueueFull:
            self._publish_terminal(
                context,
                "queue_full",
                self._now(context.started_at),
            )
            raise
        except InferenceTimedOut:
            context = self._with_queue_wait(
                context,
                self._milliseconds(
                    self._now(context.deadline)
                    - context.started_at
                ),
            )
            self._publish_terminal(
                context,
                "timeout",
                self._now(context.deadline),
            )
            raise
        except RuntimeError:
            self._publish_terminal(
                context,
                "cancelled",
                self._now(context.started_at),
            )
            raise

        lease = _AdmissionLease(self._release)
        state: _ExecutionState[Result] = _ExecutionState()
        try:
            self._lifecycle.spawn(
                target=lambda: self._execute_worker(
                    context,
                    callback,
                    state,
                ),
                name=f"inference-{context.request_id}",
                release_admission=lease.release_once,
                cancel_on_shutdown=None,
                after_release=state.done.set,
                thread_factory=Thread,
            )
        except BaseException:
            self._publish_terminal(
                context,
                "error",
                self._now(context.started_at),
            )
            raise

        try:
            remaining = max(
                0.0,
                context.deadline - self._now(context.deadline),
            )
            if not state.done.wait(remaining) and not state.done.is_set():
                if state.override("timeout", context.deadline):
                    self._publish_terminal(
                        context,
                        "timeout",
                        self._now(context.deadline),
                    )
                    raise InferenceTimedOut(
                        "inference deadline exhausted"
                    )
                state.done.wait()
            return self._execution_result(state, context)
        except _CANCELLATION_ERRORS:
            state.override("cancelled", context.deadline)
            self._publish_terminal(
                context, "cancelled", self._now(context.started_at)
            )
            raise
        except InferenceTimedOut:
            raise
        except BaseException:
            state.override("cancelled", context.deadline)
            self._publish_terminal(
                context, "cancelled", self._now(context.started_at)
            )
            raise

    def stream(
        self,
        feature: str,
        operation: str,
        snapshot: FeatureProviderSnapshot,
        iterator_factory: Callable[[], Iterator[Item]],
        cancellation: InferenceCancellation | None = None,
    ) -> Iterator[Item]:
        self._metrics.validate_dimensions(
            feature, operation, snapshot
        )
        self._ensure_open()
        if snapshot.mode == "mock":
            return self._stream_mock(
                operation=operation,
                snapshot=snapshot,
                iterator_factory=iterator_factory,
            )
        return self._stream_local(
            operation=operation,
            snapshot=snapshot,
            iterator_factory=iterator_factory,
            cancellation=cancellation,
        )

    def shutdown(self, timeout_seconds: float = 1.0) -> bool:
        if timeout_seconds < 0:
            raise ValueError("timeout_seconds must not be negative")
        callbacks = self._lifecycle.begin_shutdown()
        with self._condition:
            self._condition.notify_all()
        for cancel in callbacks:
            cancel()

        deadline = monotonic() + timeout_seconds
        workers_stopped = self._lifecycle.wait_for_quiescence(
            timeout_seconds=max(0.0, deadline - monotonic())
        )
        telemetry_stopped = self._telemetry.shutdown(
            timeout_seconds=max(0.0, deadline - monotonic())
        )
        return workers_stopped and telemetry_stopped

    def _stream_local(
        self,
        *,
        operation: str,
        snapshot: FeatureProviderSnapshot,
        iterator_factory: Callable[[], Iterator[Item]],
        cancellation: InferenceCancellation | None,
    ) -> Iterator[Item]:
        with self._lifecycle.register_request():
            yield from self._stream_local_registered(
                operation=operation,
                snapshot=snapshot,
                iterator_factory=iterator_factory,
                cancellation=cancellation,
            )

    def _stream_local_registered(
        self,
        *,
        operation: str,
        snapshot: FeatureProviderSnapshot,
        iterator_factory: Callable[[], Iterator[Item]],
        cancellation: InferenceCancellation | None,
    ) -> Iterator[Item]:
        started_at = self._clock()
        context = _RequestContext(
            request_id=str(uuid4()),
            operation=operation,
            snapshot=snapshot,
            started_at=started_at,
            deadline=started_at + self._timeout_seconds,
            queue_wait_ms=0,
        )
        try:
            context = self._with_queue_wait(
                context,
                self._acquire(
                    context.started_at,
                    context.deadline,
                    cancellation=cancellation,
                ),
            )
        except InferenceQueueFull:
            self._publish_terminal(
                context,
                "queue_full",
                self._now(context.started_at),
            )
            raise
        except InferenceTimedOut:
            context = self._with_queue_wait(
                context,
                self._milliseconds(
                    self._now(context.deadline)
                    - context.started_at
                ),
            )
            self._publish_terminal(
                context,
                "timeout",
                self._now(context.deadline),
            )
            raise
        except RuntimeError:
            self._publish_terminal(
                context,
                "cancelled",
                self._now(context.started_at),
            )
            raise

        lease = _AdmissionLease(self._release)
        state: _StreamState[Item] = _StreamState()
        outcome = _StreamWorkerOutcome()
        unregister_cancel = (
            cancellation.register_wakeup(
                lambda: self._cancel_stream(context, state)
            )
            if cancellation is not None
            else lambda: None
        )
        try:
            self._lifecycle.spawn(
                target=lambda: self._stream_worker(
                    context,
                    iterator_factory,
                    state,
                    outcome,
                    cancellation,
                ),
                name=(
                    f"inference-stream-{context.request_id}"
                ),
                release_admission=lease.release_once,
                cancel_on_shutdown=lambda: (
                    self._cancel_stream(context, state)
                ),
                after_release=lambda: self._finish_stream_worker(
                    context,
                    state,
                    outcome,
                ),
                thread_factory=Thread,
            )
        except BaseException:
            unregister_cancel()
            self._publish_terminal(
                context,
                "error",
                self._now(context.started_at),
            )
            raise

        finished = False
        try:
            while True:
                try:
                    yield state.receive(
                        deadline=context.deadline,
                        clock=self._clock,
                    )
                except StopIteration:
                    finished = True
                    return
                except FutureCancelledError as exc:
                    if (
                        cancellation is not None
                        and cancellation.is_cancelled()
                    ):
                        raise InferenceCancelled(
                            "inference request cancelled"
                        ) from exc
                    raise
                except InferenceTimedOut:
                    state.cancel("timeout")
                    self._publish_terminal(
                        context,
                        "timeout",
                        self._now(context.deadline),
                        ttft_ms=state.ttft_ms(
                            context.started_at
                        ),
                    )
                    raise
        finally:
            try:
                if not finished and state.cancel("cancelled"):
                    self._publish_terminal(
                        context,
                        "cancelled",
                        self._now(context.started_at),
                        ttft_ms=state.ttft_ms(
                            context.started_at
                        ),
                    )
            finally:
                unregister_cancel()

    def _stream_mock(
        self,
        *,
        operation: str,
        snapshot: FeatureProviderSnapshot,
        iterator_factory: Callable[[], Iterator[Item]],
    ) -> Iterator[Item]:
        with self._lifecycle.register_request():
            yield from self._stream_mock_registered(
                operation=operation,
                snapshot=snapshot,
                iterator_factory=iterator_factory,
            )

    def _stream_mock_registered(
        self,
        *,
        operation: str,
        snapshot: FeatureProviderSnapshot,
        iterator_factory: Callable[[], Iterator[Item]],
    ) -> Iterator[Item]:
        started_at = self._clock()
        context = _RequestContext(
            request_id=str(uuid4()),
            operation=operation,
            snapshot=snapshot,
            started_at=started_at,
            deadline=started_at + self._timeout_seconds,
            queue_wait_ms=0,
        )
        upstream: Iterator[Item] | None = None
        usage = _NULL_USAGE
        status = "error"
        ttft_ms: int | None = None
        terminal_error: BaseException | None = None
        try:
            self._reset_usage(snapshot)
            upstream = iter(iterator_factory())
            for item in upstream:
                observed_at = self._now(context.deadline)
                if observed_at >= context.deadline:
                    raise InferenceTimedOut(
                        "inference deadline exhausted"
                    )
                if ttft_ms is None:
                    ttft_ms = max(
                        0,
                        int(
                            (
                                observed_at - context.started_at
                            )
                            * 1000
                        ),
                    )
                yield item
            usage = self._capture_usage(snapshot)
            status = "success"
        except InferenceTimedOut as exc:
            status = "timeout"
            terminal_error = exc
            raise
        except _CANCELLATION_ERRORS as exc:
            status = "cancelled"
            terminal_error = exc
            raise
        except BaseException as exc:
            status = "error"
            terminal_error = exc
            raise
        finally:
            close_error: BaseException | None = None
            if upstream is not None:
                close = getattr(upstream, "close", None)
                if close is not None:
                    try:
                        close()
                    except BaseException as exc:
                        close_error = exc
            if terminal_error is None and close_error is not None:
                status = (
                    "cancelled"
                    if isinstance(
                        close_error, _CANCELLATION_ERRORS
                    )
                    else "error"
                )
            self._publish_terminal(
                context,
                status,
                self._now(context.started_at),
                usage=usage,
                ttft_ms=ttft_ms,
            )

    def _execute_mock(
        self,
        context: _RequestContext,
        callback: Callable[[], Result],
    ) -> Result:
        usage = _NULL_USAGE
        status = "error"
        try:
            self._reset_usage(context.snapshot)
            result = callback()
            usage = self._capture_usage(context.snapshot)
            if self._now(context.deadline) >= context.deadline:
                raise InferenceTimedOut(
                    "inference deadline exhausted"
                )
            status = "success"
            return result
        except InferenceTimedOut:
            status = "timeout"
            raise
        except _CANCELLATION_ERRORS:
            status = "cancelled"
            raise
        except BaseException:
            status = "error"
            raise
        finally:
            self._publish_terminal(
                context,
                status,
                self._now(context.started_at),
                usage=usage,
            )

    def _execute_worker(
        self,
        context: _RequestContext,
        callback: Callable[[], Result],
        state: _ExecutionState[Result],
    ) -> None:
        result: object = _MISSING
        error: BaseException | None = None
        usage = _NULL_USAGE
        base_status = "error"
        reset_succeeded = False
        completed_at = context.deadline
        try:
            now = self._now(context.deadline)
            if state.should_start(now, context.deadline):
                self._reset_usage(context.snapshot)
                reset_succeeded = True
                result = callback()
                base_status = "success"
            else:
                base_status = "timeout"
        except _CANCELLATION_ERRORS as exc:
            error = exc
            base_status = "cancelled"
        except BaseException as exc:
            error = exc
            base_status = "error"
        finally:
            if reset_succeeded:
                usage = self._capture_usage(context.snapshot)
            completed_at = self._now(context.deadline)
            with state.lock:
                state.result = result
                state.error = error
                state.usage = usage
                state.completed_at = completed_at
                state.base_status = base_status
            status = state.status(context.deadline)
            if completed_at < context.deadline:
                self._publish_terminal(
                    context,
                    status,
                    completed_at,
                    usage=usage,
                )

    def _stream_worker(
        self,
        context: _RequestContext,
        iterator_factory: Callable[[], Iterator[Item]],
        state: _StreamState[Item],
        outcome: _StreamWorkerOutcome,
        cancellation: InferenceCancellation | None = None,
    ) -> None:
        upstream: Iterator[Item] | None = None
        error: BaseException | None = None
        close_error: BaseException | None = None
        usage = _NULL_USAGE
        base_status = "error"
        reset_succeeded = False
        completed_at = context.deadline
        try:
            if (
                (
                    cancellation is None
                    or not cancellation.is_cancelled()
                )
                and
                not state.is_cancelled()
                and self._now(context.deadline)
                < context.deadline
            ):
                self._reset_usage(context.snapshot)
                reset_succeeded = True
                if (
                    cancellation is not None
                    and not cancellation.claim_provider_start()
                ):
                    error = InferenceCancelled(
                        "inference request cancelled"
                    )
                    base_status = "cancelled"
                else:
                    upstream = iter(iterator_factory())
                    while not state.is_cancelled():
                        try:
                            item = next(upstream)
                        except StopIteration:
                            base_status = "success"
                            break
                        if not state.publish(
                            item, self._now(context.deadline)
                        ):
                            base_status = "cancelled"
                            break
            elif (
                state.is_cancelled()
                or (
                    cancellation is not None
                    and cancellation.is_cancelled()
                )
            ):
                base_status = "cancelled"
            else:
                base_status = "timeout"
        except _CANCELLATION_ERRORS as exc:
            error = exc
            base_status = "cancelled"
        except BaseException as exc:
            error = exc
            base_status = "error"
        finally:
            try:
                if upstream is not None:
                    close = getattr(upstream, "close", None)
                    if close is not None:
                        try:
                            close()
                        except BaseException as exc:
                            close_error = exc
                if error is None and close_error is not None:
                    error = close_error
                    base_status = (
                        "cancelled"
                        if isinstance(
                            close_error, _CANCELLATION_ERRORS
                        )
                        else "error"
                    )
                state.wait_until_drained()
                if reset_succeeded:
                    usage = self._capture_usage(context.snapshot)
                completed_at = self._now(context.deadline)
            finally:
                outcome.error = error
                outcome.usage = usage
                outcome.completed_at = completed_at
                outcome.base_status = base_status

    def _finish_stream_worker(
        self,
        context: _RequestContext,
        state: _StreamState[Item],
        outcome: _StreamWorkerOutcome,
    ) -> None:
        state.finish(
            error=outcome.error,
            usage=outcome.usage,
            completed_at=outcome.completed_at,
            base_status=outcome.base_status,
        )
        status = state.status(context.deadline)
        if outcome.completed_at < context.deadline:
            self._publish_terminal(
                context,
                status,
                outcome.completed_at,
                usage=outcome.usage,
                ttft_ms=state.ttft_ms(
                    context.started_at
                ),
            )

    def _cancel_stream(
        self,
        context: _RequestContext,
        state: _StreamState[Item],
    ) -> None:
        if state.cancel("cancelled"):
            self._publish_terminal(
                context,
                "cancelled",
                self._now(context.started_at),
                ttft_ms=state.ttft_ms(
                    context.started_at
                ),
            )

    def _execution_result(
        self,
        state: _ExecutionState[Result],
        context: _RequestContext,
    ) -> Result:
        status = state.status(context.deadline)
        if status == "timeout":
            self._publish_terminal(
                context, "timeout", self._now(context.deadline)
            )
            raise InferenceTimedOut(
                "inference deadline exhausted"
            )
        with state.lock:
            error = state.error
            result = state.result
        if error is not None:
            raise error
        return cast(Result, result)

    def _acquire(
        self,
        started_at: float,
        deadline: float,
        cancellation: InferenceCancellation | None = None,
    ) -> int:
        queued = False
        unregister_wakeup = (
            cancellation.register_wakeup(self._wake_acquisition)
            if cancellation is not None
            else lambda: None
        )
        try:
            with self._condition:
                if self._lifecycle.closing:
                    raise RuntimeError(
                        "inference coordinator is shut down"
                    )
                if self._clock() >= deadline:
                    raise InferenceTimedOut(
                        "inference deadline exhausted"
                    )
                if (
                    cancellation is not None
                    and cancellation.is_cancelled()
                ):
                    raise InferenceCancelled(
                        "inference request cancelled"
                    )
                if self._running < self._concurrency:
                    self._claim_admission(cancellation)
                    self._running += 1
                    return self._milliseconds(
                        self._clock() - started_at
                    )
                if self._queued >= self._max_queue:
                    raise InferenceQueueFull(
                        "local inference queue is full"
                    )
                self._queued += 1
                queued = True
                try:
                    while self._running >= self._concurrency:
                        if (
                            cancellation is not None
                            and cancellation.is_cancelled()
                        ):
                            raise InferenceCancelled(
                                "inference request cancelled"
                            )
                        if self._lifecycle.closing:
                            raise RuntimeError(
                                "inference coordinator is shut down"
                            )
                        remaining = deadline - self._clock()
                        if remaining <= 0:
                            raise InferenceTimedOut(
                                "inference deadline exhausted"
                            )
                        self._condition.wait(remaining)
                    if self._lifecycle.closing:
                        raise RuntimeError(
                            "inference coordinator is shut down"
                        )
                    if self._clock() >= deadline:
                        raise InferenceTimedOut(
                            "inference deadline exhausted"
                        )
                    self._claim_admission(cancellation)
                    self._queued -= 1
                    queued = False
                    self._running += 1
                    return self._milliseconds(
                        self._clock() - started_at
                    )
                finally:
                    if queued:
                        self._queued -= 1
                        self._condition.notify_all()
        finally:
            unregister_wakeup()

    @staticmethod
    def _claim_admission(
        cancellation: InferenceCancellation | None,
    ) -> None:
        if (
            cancellation is not None
            and not cancellation.claim_admission()
        ):
            raise InferenceCancelled("inference request cancelled")

    def _wake_acquisition(self) -> None:
        with self._condition:
            self._condition.notify_all()

    def _release(self) -> None:
        with self._condition:
            self._running -= 1
            self._condition.notify_all()

    def _publish_terminal(
        self,
        context: _RequestContext,
        status: str,
        terminal_at: float,
        *,
        usage: ProviderUsage = _NULL_USAGE,
        ttft_ms: int | None = None,
    ) -> bool:
        if not context.terminal.claim():
            return False
        metric = self._metrics.event(
            request_id=context.request_id,
            operation=context.operation,
            snapshot=context.snapshot,
            started_at=context.started_at,
            terminal_at=terminal_at,
            queue_wait_ms=context.queue_wait_ms,
            ttft_ms=ttft_ms,
            usage=usage,
            status=status,
        )
        self._telemetry.submit(metric)
        return True

    def _ensure_open(self) -> None:
        self._lifecycle.ensure_open()

    @staticmethod
    def _with_queue_wait(
        context: _RequestContext,
        queue_wait_ms: int,
    ) -> _RequestContext:
        return _RequestContext(
            request_id=context.request_id,
            operation=context.operation,
            snapshot=context.snapshot,
            started_at=context.started_at,
            deadline=context.deadline,
            queue_wait_ms=queue_wait_ms,
            terminal=context.terminal,
        )

    @staticmethod
    def _reset_usage(snapshot: FeatureProviderSnapshot) -> None:
        snapshot.provider.reset_usage()

    @staticmethod
    def _capture_usage(
        snapshot: FeatureProviderSnapshot,
    ) -> ProviderUsage:
        try:
            usage = snapshot.provider.last_usage
        except BaseException:
            return _NULL_USAGE
        return usage if isinstance(usage, ProviderUsage) else _NULL_USAGE

    def _now(self, fallback: float) -> float:
        try:
            return self._clock()
        except BaseException:
            return fallback

    @staticmethod
    def _milliseconds(seconds: float) -> int:
        return max(0, int(seconds * 1000))
