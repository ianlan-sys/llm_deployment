from collections.abc import Callable
from dataclasses import dataclass
from queue import Full, Queue
from threading import Lock, Thread
from typing import Any

from sqlalchemy import Engine, create_engine
from sqlalchemy.orm import sessionmaker

from app.models import ModelRequestMetric
from app.providers.base import ProviderUsage
from app.providers.feature_router import (
    FEATURES,
    FeatureProviderSnapshot,
)


APPROVED_MODEL = "qwen3:1.7b-q4_K_M"
ALLOWED_OPERATIONS = frozenset(
    {
        "call_suggestion",
        "call_summary",
        "knowledge_answer",
        "document_review",
    }
)
_STOP = object()


@dataclass(frozen=True)
class InferenceStats:
    request_id: str
    queue_wait_ms: int
    ttft_ms: int | None
    elapsed_ms: int
    input_tokens: int | None
    output_tokens: int | None
    result_status: str


@dataclass(frozen=True)
class _InferenceMetricEvent:
    """An immutable, content-free persistence event."""

    feature: str
    mode: str
    model: str
    request_id: str
    operation: str
    queue_wait_ms: int
    ttft_ms: int | None
    elapsed_ms: int
    input_tokens: int | None
    output_tokens: int | None
    result_status: str


@dataclass(frozen=True)
class _TelemetryState:
    accepting: bool
    queued: int
    in_flight: int
    terminal: bool


class _SqlAlchemyMetricWriter:
    """Owns the engine used exclusively by the telemetry thread."""

    def __init__(self, database_url: str):
        connect_args: dict[str, object] = {}
        if database_url.startswith("sqlite"):
            connect_args = {
                "check_same_thread": False,
                "timeout": 0.1,
            }
        self.engine = create_engine(
            database_url,
            connect_args=connect_args,
        )
        self._session_factory = sessionmaker(bind=self.engine)
        self._lock = Lock()
        self._closed = False

    @property
    def closed(self) -> bool:
        with self._lock:
            return self._closed

    def persist(self, metric: _InferenceMetricEvent) -> None:
        _persist_event(self._session_factory, metric)

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
        self.engine.dispose()


class _InferenceTelemetry:
    """Bounded nonblocking event submission with one database writer."""

    def __init__(
        self,
        persist: Callable[[_InferenceMetricEvent], None],
        *,
        close: Callable[[], None],
        capacity: int = 256,
    ):
        if capacity <= 0:
            raise ValueError("capacity must be greater than zero")
        self._persist = persist
        self._close = close
        self._queue: Queue[object] = Queue(maxsize=capacity)
        self._lock = Lock()
        self._accepting = True
        self._queued = 0
        self._in_flight = 0
        self._terminal = False
        self._writer: Thread | None = None
        try:
            writer = Thread(
                target=self._run,
                name="inference-telemetry",
                daemon=True,
            )
            self._writer = writer
            writer.start()
        except BaseException:
            with self._lock:
                self._accepting = False
                writer_alive = self._is_writer_alive()
                if not writer_alive:
                    self._terminal = True
            if writer_alive:
                try:
                    self._queue.put_nowait(_STOP)
                except Full:
                    pass
            else:
                try:
                    self._close()
                except BaseException:
                    pass

    def submit(self, metric: _InferenceMetricEvent) -> bool:
        with self._lock:
            if not self._accepting:
                return False
            try:
                self._queue.put_nowait(metric)
            except Full:
                return False
            self._queued += 1
            return True

    def state(self) -> _TelemetryState:
        with self._lock:
            return _TelemetryState(
                accepting=self._accepting,
                queued=self._queued,
                in_flight=self._in_flight,
                terminal=self._terminal,
            )

    def shutdown(self, timeout_seconds: float = 1.0) -> bool:
        if timeout_seconds < 0:
            raise ValueError("timeout_seconds must not be negative")
        with self._lock:
            if self._accepting:
                self._accepting = False
                try:
                    self._queue.put_nowait(_STOP)
                except Full:
                    pass
        writer = self._writer
        if writer is not None and self._is_writer_alive():
            writer.join(timeout_seconds)
        with self._lock:
            return self._terminal

    def _run(self) -> None:
        try:
            while True:
                metric = self._queue.get()
                if metric is _STOP:
                    break
                with self._lock:
                    self._queued -= 1
                    self._in_flight += 1
                try:
                    try:
                        self._persist(metric)
                    except BaseException:
                        pass
                finally:
                    with self._lock:
                        self._in_flight -= 1
                        finished = (
                            not self._accepting
                            and self._queued == 0
                        )
                if finished:
                    break
        finally:
            try:
                self._close()
            except BaseException:
                pass
            with self._lock:
                self._terminal = True

    def _is_writer_alive(self) -> bool:
        writer = self._writer
        if writer is None:
            return False
        try:
            return writer.is_alive()
        except BaseException:
            return True


class _DropOnlyInferenceTelemetry:
    """A closed sink used when no independent database URL exists."""

    @staticmethod
    def submit(_metric: _InferenceMetricEvent) -> bool:
        return False

    @staticmethod
    def state() -> _TelemetryState:
        return _TelemetryState(
            accepting=False,
            queued=0,
            in_flight=0,
            terminal=True,
        )

    @staticmethod
    def shutdown(timeout_seconds: float = 1.0) -> bool:
        if timeout_seconds < 0:
            raise ValueError(
                "timeout_seconds must not be negative"
            )
        return True


class _InferenceMetricRecorder:
    """Validates dimensions and creates sanitized metric events only."""

    @staticmethod
    def validate_dimensions(
        feature: str,
        operation: str,
        snapshot: FeatureProviderSnapshot,
    ) -> None:
        if (
            feature not in FEATURES
            or snapshot.feature not in FEATURES
            or feature != snapshot.feature
        ):
            raise ValueError(
                "feature must match the provider snapshot"
            )
        if operation not in ALLOWED_OPERATIONS:
            raise ValueError("operation is not allowed")
        if snapshot.mode not in {"mock", "local"}:
            raise ValueError("mode must be mock or local")

    @classmethod
    def event(
        cls,
        *,
        request_id: str,
        operation: str,
        snapshot: FeatureProviderSnapshot,
        started_at: float,
        terminal_at: float,
        queue_wait_ms: int,
        ttft_ms: int | None,
        usage: ProviderUsage,
        status: str,
    ) -> _InferenceMetricEvent:
        stats = cls._stats(
            request_id=request_id,
            started_at=started_at,
            terminal_at=terminal_at,
            queue_wait_ms=queue_wait_ms,
            ttft_ms=ttft_ms,
            usage=usage,
            status=status,
        )
        return _InferenceMetricEvent(
            feature=snapshot.feature,
            mode=snapshot.mode,
            model=cls._metric_model(snapshot),
            request_id=stats.request_id,
            operation=operation,
            queue_wait_ms=stats.queue_wait_ms,
            ttft_ms=stats.ttft_ms,
            elapsed_ms=stats.elapsed_ms,
            input_tokens=stats.input_tokens,
            output_tokens=stats.output_tokens,
            result_status=stats.result_status,
        )

    @classmethod
    def _stats(
        cls,
        *,
        request_id: str,
        started_at: float,
        terminal_at: float,
        queue_wait_ms: int,
        ttft_ms: int | None,
        usage: ProviderUsage,
        status: str,
    ) -> InferenceStats:
        return InferenceStats(
            request_id=request_id,
            queue_wait_ms=queue_wait_ms,
            ttft_ms=ttft_ms,
            elapsed_ms=cls._milliseconds(
                terminal_at - started_at
            ),
            input_tokens=usage.input_tokens,
            output_tokens=usage.output_tokens,
            result_status=status,
        )

    @staticmethod
    def _metric_model(snapshot: FeatureProviderSnapshot) -> str:
        if snapshot.mode == "mock":
            return "mock"
        if snapshot.model == APPROVED_MODEL:
            return APPROVED_MODEL
        return "local"

    @staticmethod
    def _milliseconds(seconds: float) -> int:
        return max(0, int(seconds * 1000))


def _persist_event(
    session_factory: Callable[[], Any],
    metric: _InferenceMetricEvent,
) -> None:
    with session_factory() as session:
        session.add(
            ModelRequestMetric(
                feature=metric.feature,
                mode=metric.mode,
                model=metric.model,
                request_id=metric.request_id,
                operation=metric.operation,
                queue_wait_ms=metric.queue_wait_ms,
                ttft_ms=metric.ttft_ms,
                elapsed_ms=metric.elapsed_ms,
                input_tokens=metric.input_tokens,
                output_tokens=metric.output_tokens,
                result_status=metric.result_status,
            )
        )
        session.commit()


def _create_inference_telemetry(
    session_factory: Callable[[], Any],
    database_url: str | None,
) -> _InferenceTelemetry | _DropOnlyInferenceTelemetry:
    resolved_url = database_url or _database_url(session_factory)
    if resolved_url is None:
        return _DropOnlyInferenceTelemetry()
    writer = _SqlAlchemyMetricWriter(resolved_url)
    return _InferenceTelemetry(
        writer.persist,
        close=writer.close,
    )


def _database_url(
    session_factory: Callable[[], Any],
) -> str | None:
    keywords = getattr(session_factory, "kw", None)
    if not isinstance(keywords, dict):
        return None
    bind = keywords.get("bind")
    if not isinstance(bind, Engine):
        return None
    return str(bind.url)
