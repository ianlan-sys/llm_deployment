from collections.abc import Callable
from dataclasses import dataclass
from threading import Condition, Event, Lock, Thread, current_thread
from time import monotonic
from typing import Any


class _ReleaseOnce:
    def __init__(self, release: Callable[[], Any]):
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


class _StartHandshake:
    def __init__(self):
        self._event = Event()
        self._allowed = False

    def allow(self) -> None:
        self._allowed = True
        self._event.set()

    def abort(self) -> None:
        self._allowed = False
        self._event.set()

    def wait(self) -> bool:
        self._event.wait()
        return self._allowed


class _RequestRegistration:
    def __init__(self, lifecycle: "_LocalWorkerLifecycle"):
        self._lifecycle = lifecycle
        self._release = _ReleaseOnce(
            lifecycle._release_request
        )

    def release_once(self) -> bool:
        return self._release.release_once()

    def __enter__(self) -> "_RequestRegistration":
        return self

    def __exit__(
        self,
        _error_type: type[BaseException] | None,
        _error: BaseException | None,
        _traceback: object,
    ) -> None:
        self.release_once()


@dataclass(frozen=True)
class _WorkerRegistration:
    cancel_on_shutdown: Callable[[], None] | None


class _LocalWorkerLifecycle:
    """Linearizes request acceptance, worker start, and shutdown."""

    def __init__(self):
        self._condition = Condition()
        self._closing_event = Event()
        self._requests = 0
        self._workers: dict[Thread, _WorkerRegistration] = {}

    @property
    def closing(self) -> bool:
        return self._closing_event.is_set()

    def ensure_open(self) -> None:
        with self._condition:
            if self._closing_event.is_set():
                raise RuntimeError(
                    "inference coordinator is shut down"
                )

    def register_request(self) -> _RequestRegistration:
        with self._condition:
            if self._closing_event.is_set():
                raise RuntimeError(
                    "inference coordinator is shut down"
                )
            self._requests += 1
        return _RequestRegistration(self)

    def spawn(
        self,
        *,
        target: Callable[[], None],
        name: str,
        release_admission: Callable[[], Any],
        cancel_on_shutdown: Callable[[], None] | None,
        after_release: Callable[[], None] | None = None,
        thread_factory: Callable[..., Thread] = Thread,
    ) -> Thread:
        admission = _ReleaseOnce(release_admission)
        gate = _StartHandshake()
        worker: Thread | None = None
        error: BaseException | None = None

        def run() -> None:
            allowed = False
            try:
                allowed = gate.wait()
                if allowed:
                    target()
            finally:
                try:
                    admission.release_once()
                    if allowed and after_release is not None:
                        after_release()
                finally:
                    self._unregister_current_worker()

        with self._condition:
            if self._closing_event.is_set():
                error = RuntimeError(
                    "inference coordinator is shut down"
                )
            else:
                try:
                    worker = thread_factory(
                        target=run,
                        name=name,
                        daemon=True,
                    )
                    self._workers[worker] = _WorkerRegistration(
                        cancel_on_shutdown=cancel_on_shutdown
                    )
                    try:
                        worker.start()
                    except BaseException as exc:
                        error = exc
                        gate.abort()
                        if not self._is_alive(worker):
                            self._workers.pop(worker, None)
                            self._condition.notify_all()
                    else:
                        gate.allow()
                except BaseException as exc:
                    error = exc

        if error is not None:
            gate.abort()
            admission.release_once()
            raise error
        if worker is None:
            admission.release_once()
            raise RuntimeError("worker construction failed")
        return worker

    def begin_shutdown(self) -> tuple[Callable[[], None], ...]:
        with self._condition:
            self._closing_event.set()
            callbacks = tuple(
                registration.cancel_on_shutdown
                for registration in self._workers.values()
                if registration.cancel_on_shutdown is not None
            )
            self._condition.notify_all()
            return callbacks

    def wait_for_quiescence(
        self,
        timeout_seconds: float,
    ) -> bool:
        if timeout_seconds < 0:
            raise ValueError(
                "timeout_seconds must not be negative"
            )
        deadline = monotonic() + timeout_seconds
        with self._condition:
            while self._requests or self._workers:
                remaining = deadline - monotonic()
                if remaining <= 0:
                    return False
                self._condition.wait(remaining)
            return True

    def _release_request(self) -> None:
        with self._condition:
            self._requests -= 1
            self._condition.notify_all()

    def _unregister_current_worker(self) -> None:
        worker = current_thread()
        with self._condition:
            self._workers.pop(worker, None)
            self._condition.notify_all()

    @staticmethod
    def _is_alive(worker: Thread) -> bool:
        try:
            return worker.is_alive()
        except BaseException:
            return True
