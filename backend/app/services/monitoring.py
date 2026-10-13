import math
from collections import defaultdict
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from threading import Event, Lock, Thread, current_thread
from typing import Literal

import psutil
from sqlalchemy import text

from app.models import ResourceSample
from app.schemas import (
    MonitoringHistoryResponse,
    MonitoringRequestPoint,
    MonitoringResourcePoint,
)


SamplingStatus = Literal["idle", "ok", "sampling_error"]
RETENTION_HOURS = 24
SAMPLE_INTERVAL_SECONDS = 10.0
CANDIDATE_MARGIN = timedelta(seconds=1)
CANONICAL_TIMESTAMP_GLOB = (
    "[0-9][0-9][0-9][0-9]-"
    "[0-9][0-9]-[0-9][0-9] "
    "[0-9][0-9]:[0-9][0-9]:[0-9][0-9]."
    "[0-9][0-9][0-9][0-9][0-9][0-9]"
)
VALID_TIMESTAMP_SQL = (
    "typeof(created_at) = 'text' "
    "AND length(created_at) = 26 "
    f"AND created_at GLOB '{CANONICAL_TIMESTAMP_GLOB}' "
    "AND julianday(created_at) IS NOT NULL"
    " AND substr(created_at, 1, 19) = "
    "strftime("
    "'%Y-%m-%d %H:%M:%S', "
    "julianday(substr(created_at, 1, 19))"
    ")"
)
SAFE_RESULT_STATUSES = frozenset(
    {
        "success",
        "error",
        "timeout",
        "cancelled",
        "queue_full",
        "shutdown",
    }
)
SAFE_FEATURES = frozenset(
    {"call_assistant", "knowledge", "document_review"}
)
SAFE_MODES = frozenset({"mock", "local"})


@dataclass(frozen=True)
class _MetricRow:
    feature: str
    mode: str
    elapsed_ms: int
    ttft_ms: int | None
    queue_wait_ms: int
    input_tokens: int | None
    output_tokens: int | None
    result_status: str
    created_at: datetime


def percentile_95(values: list[int]) -> int:
    if not values:
        return 0
    ordered = sorted(values)
    index = max(math.ceil(0.95 * len(ordered)) - 1, 0)
    return ordered[index]


class MonitoringService:
    """Builds content-free monitoring aggregates from telemetry rows."""

    def __init__(
        self,
        session_factory: Callable[[], object],
        *,
        clock: Callable[[], datetime] | None = None,
    ):
        self._session_factory = session_factory
        self._clock = clock or _utc_now

    def history(self, hours: int = RETENTION_HOURS) -> MonitoringHistoryResponse:
        if isinstance(hours, bool) or not 1 <= hours <= RETENTION_HOURS:
            raise ValueError("hours must be between 1 and 24")
        window_end = _as_utc(self._clock())
        cutoff = window_end - timedelta(hours=hours)
        candidates = {
            "candidate_start": _sqlite_datetime(
                cutoff - CANDIDATE_MARGIN
            ),
            "candidate_end": _sqlite_datetime(
                window_end + CANDIDATE_MARGIN
            ),
        }
        with self._session_factory() as session:
            metric_rows = session.execute(
                text(
                    "SELECT feature, mode, elapsed_ms, ttft_ms, "
                    "queue_wait_ms, input_tokens, output_tokens, "
                    "result_status, created_at "
                    "FROM model_request_metrics "
                    f"WHERE {VALID_TIMESTAMP_SQL} "
                    "AND julianday(created_at) BETWEEN "
                    "julianday(:candidate_start) "
                    "AND julianday(:candidate_end)"
                ),
                candidates,
            ).mappings()
            metrics = [
                metric
                for row in metric_rows
                if (
                    metric := _safe_metric_row(
                        row, cutoff, window_end
                    )
                )
                is not None
            ]
            resource_rows = session.execute(
                text(
                    "SELECT cpu_percent, memory_percent, disk_percent, "
                    "ollama_available, model_loaded, running_count, "
                    "queued_count, created_at "
                    "FROM resource_samples "
                    f"WHERE {VALID_TIMESTAMP_SQL} "
                    "AND julianday(created_at) BETWEEN "
                    "julianday(:candidate_start) "
                    "AND julianday(:candidate_end)"
                ),
                candidates,
            ).mappings()
            resources = [
                point
                for row in resource_rows
                if (
                    point := _safe_resource_point(
                        row, cutoff, window_end
                    )
                )
                is not None
            ]
        metrics.sort(key=lambda metric: metric.created_at)
        resources.sort(key=lambda point: point.created_at)

        elapsed = [metric.elapsed_ms for metric in metrics]
        ttft = [
            metric.ttft_ms
            for metric in metrics
            if metric.ttft_ms is not None
        ]
        queue_wait = [metric.queue_wait_ms for metric in metrics]
        input_tokens = [
            metric.input_tokens
            for metric in metrics
            if metric.input_tokens is not None
        ]
        output_tokens = [
            metric.output_tokens
            for metric in metrics
            if metric.output_tokens is not None
        ]
        return MonitoringHistoryResponse(
            hours=hours,
            total_requests=len(metrics),
            average_elapsed_ms=_average(elapsed),
            p95_elapsed_ms=percentile_95(elapsed),
            average_ttft_ms=_optional_average(ttft),
            average_queue_wait_ms=_average(queue_wait),
            max_queue_depth=max(
                (point.queued_count for point in resources),
                default=0,
            ),
            input_tokens=sum(input_tokens) if input_tokens else None,
            output_tokens=sum(output_tokens) if output_tokens else None,
            requests=_request_groups(metrics),
            resources=resources,
        )


class ResourceSampler:
    """Samples host resources without access to a container runtime socket."""

    def __init__(
        self,
        session_factory: Callable[[], object],
        *,
        ollama_status: Callable[[], object],
        inference_state: Callable[[], object],
        clock: Callable[[], datetime] | None = None,
        interval_seconds: float = SAMPLE_INTERVAL_SECONDS,
    ):
        if interval_seconds <= 0:
            raise ValueError("interval_seconds must be greater than zero")
        self._session_factory = session_factory
        self._ollama_status = ollama_status
        self._inference_state = inference_state
        self._clock = clock or _utc_now
        self._interval_seconds = interval_seconds
        self._stop_event = Event()
        self._lock = Lock()
        self._thread: Thread | None = None
        self._stopping = 0
        self._status: SamplingStatus = "idle"

    @property
    def status(self) -> SamplingStatus:
        with self._lock:
            return self._status

    def start(self) -> None:
        thread: Thread | None = None
        cleanup_required = False
        try:
            with self._lock:
                if self._stopping:
                    return
                if (
                    self._thread is not None
                    and self._thread.is_alive()
                ):
                    return
                self._thread = None
                self._stop_event.clear()
                try:
                    psutil.cpu_percent()
                except Exception:
                    self._status = "sampling_error"
                try:
                    thread = Thread(
                        target=self._run,
                        name="resource-sampler",
                        daemon=True,
                    )
                    self._thread = thread
                    thread.start()
                except Exception:
                    self._status = "sampling_error"
                    if thread is not None:
                        self._begin_failed_start_cleanup()
                        cleanup_required = True
                except BaseException:
                    if thread is not None:
                        self._begin_failed_start_cleanup()
                        cleanup_required = True
                    raise
        finally:
            if thread is not None and cleanup_required:
                self._cleanup_failed_start(thread)

    def _begin_failed_start_cleanup(self) -> None:
        self._stopping += 1
        self._stop_event.set()

    def _cleanup_failed_start(self, thread: Thread) -> None:
        try:
            if thread is not current_thread() and thread.is_alive():
                thread.join()
        finally:
            with self._lock:
                if self._thread is thread and not thread.is_alive():
                    self._thread = None
                self._stopping -= 1
                self._status = "sampling_error"

    def stop(self) -> None:
        with self._lock:
            self._stopping += 1
            self._stop_event.set()
            thread = self._thread
        try:
            if thread is not None and thread is not current_thread():
                thread.join()
        finally:
            with self._lock:
                if self._thread is thread and (
                    thread is None or not thread.is_alive()
                ):
                    self._thread = None
                self._stopping -= 1

    def sample_once(self) -> MonitoringResourcePoint | None:
        try:
            ollama_available, model_loaded = self._ollama_flags()
            inference = self._inference_state()
            sample = ResourceSample(
                cpu_percent=_percentage(psutil.cpu_percent()),
                memory_percent=_percentage(psutil.virtual_memory().percent),
                disk_percent=_percentage(psutil.disk_usage("/").percent),
                ollama_available=ollama_available,
                model_loaded=model_loaded,
                running_count=_counter(inference, "running"),
                queued_count=_counter(inference, "queued"),
                created_at=_as_utc(self._clock()),
            )
            point = MonitoringResourcePoint(
                created_at=sample.created_at,
                cpu_percent=sample.cpu_percent,
                memory_percent=sample.memory_percent,
                disk_percent=sample.disk_percent,
                ollama_available=sample.ollama_available,
                model_loaded=sample.model_loaded,
                running_count=sample.running_count,
                queued_count=sample.queued_count,
            )
            with self._session_factory() as session:
                session.add(sample)
                session.commit()
        except Exception:
            self._set_status("sampling_error")
            return None

        self._set_status("ok")
        self._cleanup()
        return point

    def _run(self) -> None:
        while not self._stop_event.wait(self._interval_seconds):
            self.sample_once()

    def _ollama_flags(self) -> tuple[bool, bool]:
        try:
            health = self._ollama_status()
        except Exception:
            return False, False
        return (
            bool(getattr(health, "available", False)),
            bool(getattr(health, "loaded", False)),
        )

    def _cleanup(self) -> None:
        cutoff = _as_utc(self._clock()) - timedelta(hours=RETENTION_HOURS)
        try:
            with self._session_factory() as session:
                _cleanup_table(
                    session,
                    "model_request_metrics",
                    cutoff,
                )
                _cleanup_table(
                    session,
                    "resource_samples",
                    cutoff,
                )
                session.commit()
        except Exception:
            return

    def _set_status(self, status: SamplingStatus) -> None:
        with self._lock:
            self._status = status


def _request_groups(
    metrics: list[_MetricRow],
) -> list[MonitoringRequestPoint]:
    grouped: dict[
        tuple[datetime, str, str, str],
        list[_MetricRow],
    ] = defaultdict(list)
    for metric in metrics:
        created_at = _as_utc(metric.created_at)
        bucket = created_at.replace(
            minute=created_at.minute - created_at.minute % 5,
            second=0,
            microsecond=0,
        )
        grouped[
            (
                bucket,
                metric.feature,
                metric.mode,
                _safe_result_status(metric.result_status),
            )
        ].append(metric)

    points = []
    for (
        bucket,
        feature,
        mode,
        result_status,
    ), rows in sorted(grouped.items()):
        elapsed = [row.elapsed_ms for row in rows]
        ttft = [row.ttft_ms for row in rows if row.ttft_ms is not None]
        queue_wait = [row.queue_wait_ms for row in rows]
        input_tokens = [
            row.input_tokens for row in rows if row.input_tokens is not None
        ]
        output_tokens = [
            row.output_tokens for row in rows if row.output_tokens is not None
        ]
        points.append(
            MonitoringRequestPoint(
                created_at=bucket,
                feature=feature,
                mode=mode,
                result_status=result_status,
                request_count=len(rows),
                average_elapsed_ms=_average(elapsed),
                p95_elapsed_ms=percentile_95(elapsed),
                average_ttft_ms=_optional_average(ttft),
                average_queue_wait_ms=_average(queue_wait),
                input_tokens=sum(input_tokens) if input_tokens else None,
                output_tokens=sum(output_tokens) if output_tokens else None,
            )
        )
    return points


def _average(values: list[int]) -> float:
    return sum(values) / len(values) if values else 0.0


def _optional_average(values: list[int]) -> float | None:
    return _average(values) if values else None


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _sqlite_datetime(value: datetime) -> str:
    return _as_utc(value).strftime("%Y-%m-%d %H:%M:%S.%f")


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _percentage(value: object) -> float:
    number = float(value)
    if not math.isfinite(number) or not 0 <= number <= 100:
        raise ValueError("invalid resource percentage")
    return number


def _counter(state: object, name: str) -> int:
    value = getattr(state, name)
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError("invalid inference counter")
    return value


def _safe_result_status(status: object) -> str:
    return (
        status
        if isinstance(status, str) and status in SAFE_RESULT_STATUSES
        else "error"
    )


def _safe_metric_row(
    row: Mapping[str, object],
    cutoff: datetime,
    window_end: datetime,
) -> _MetricRow | None:
    created_at = _safe_datetime(row.get("created_at"))
    feature = row.get("feature")
    mode = row.get("mode")
    elapsed_ms = row.get("elapsed_ms")
    queue_wait_ms = row.get("queue_wait_ms")
    ttft_ms = row.get("ttft_ms")
    input_tokens = row.get("input_tokens")
    output_tokens = row.get("output_tokens")
    if not (
        created_at is not None
        and cutoff <= created_at <= window_end
        and isinstance(feature, str)
        and feature in SAFE_FEATURES
        and isinstance(mode, str)
        and mode in SAFE_MODES
        and _valid_nonnegative_int(elapsed_ms)
        and _valid_nonnegative_int(queue_wait_ms)
        and _valid_optional_nonnegative_int(ttft_ms)
        and _valid_optional_nonnegative_int(input_tokens)
        and _valid_optional_nonnegative_int(output_tokens)
    ):
        return None
    return _MetricRow(
        feature=feature,
        mode=mode,
        elapsed_ms=elapsed_ms,
        ttft_ms=ttft_ms,
        queue_wait_ms=queue_wait_ms,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        result_status=_safe_result_status(row.get("result_status")),
        created_at=created_at,
    )


def _safe_resource_point(
    row: Mapping[str, object],
    cutoff: datetime,
    window_end: datetime,
) -> MonitoringResourcePoint | None:
    created_at = _safe_datetime(row.get("created_at"))
    cpu_percent = row.get("cpu_percent")
    memory_percent = row.get("memory_percent")
    disk_percent = row.get("disk_percent")
    ollama_available = _safe_bool(row.get("ollama_available"))
    model_loaded = _safe_bool(row.get("model_loaded"))
    running_count = row.get("running_count")
    queued_count = row.get("queued_count")
    if not (
        created_at is not None
        and cutoff <= created_at <= window_end
        and _valid_percentage(cpu_percent)
        and _valid_percentage(memory_percent)
        and _valid_percentage(disk_percent)
        and ollama_available is not None
        and model_loaded is not None
        and _valid_nonnegative_int(running_count)
        and _valid_nonnegative_int(queued_count)
    ):
        return None
    try:
        return MonitoringResourcePoint(
            created_at=created_at,
            cpu_percent=cpu_percent,
            memory_percent=memory_percent,
            disk_percent=disk_percent,
            ollama_available=ollama_available,
            model_loaded=model_loaded,
            running_count=running_count,
            queued_count=queued_count,
        )
    except Exception:
        return None


def _valid_nonnegative_int(value: object) -> bool:
    return (
        isinstance(value, int)
        and not isinstance(value, bool)
        and value >= 0
    )


def _valid_optional_nonnegative_int(value: object) -> bool:
    return value is None or _valid_nonnegative_int(value)


def _valid_percentage(value: object) -> bool:
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(value)
        and 0 <= value <= 100
    )


def _safe_bool(value: object) -> bool | None:
    if isinstance(value, bool):
        return value
    if isinstance(value, int) and not isinstance(value, bool):
        if value in {0, 1}:
            return bool(value)
    return None


def _safe_datetime(value: object) -> datetime | None:
    if isinstance(value, datetime):
        return _as_utc(value)
    if not isinstance(value, str):
        return None
    normalized = (
        f"{value[:-1]}+00:00"
        if value.endswith(("Z", "z"))
        else value
    )
    try:
        return _as_utc(datetime.fromisoformat(normalized))
    except (TypeError, ValueError, OverflowError):
        return None


def _cleanup_table(
    session: object,
    table_name: str,
    cutoff: datetime,
) -> None:
    boundary_start = cutoff - CANDIDATE_MARGIN
    parameters = {
        "boundary_start": _sqlite_datetime(boundary_start),
        "boundary_end": _sqlite_datetime(
            cutoff + CANDIDATE_MARGIN
        ),
    }
    session.execute(
        text(
            f"DELETE FROM {table_name} "
            f"WHERE NOT ({VALID_TIMESTAMP_SQL}) "
            "OR julianday(created_at) < "
            "julianday(:boundary_start)"
        ),
        parameters,
    )
    boundary_rows = session.execute(
        text(
            f"SELECT rowid AS cleanup_rowid, created_at "
            f"FROM {table_name} "
            f"WHERE {VALID_TIMESTAMP_SQL} "
            "AND julianday(created_at) BETWEEN "
            "julianday(:boundary_start) "
            "AND julianday(:boundary_end)"
        ),
        parameters,
    ).mappings()
    expired_rowids = [
        row["cleanup_rowid"]
        for row in boundary_rows
        if (
            (created_at := _safe_datetime(row.get("created_at")))
            is None
            or created_at < cutoff
        )
    ]
    for rowid in expired_rowids:
        session.execute(
            text(
                f"DELETE FROM {table_name} "
                "WHERE rowid = :cleanup_rowid"
            ),
            {"cleanup_rowid": rowid},
        )
