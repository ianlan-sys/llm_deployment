from concurrent.futures import Future, ThreadPoolExecutor
from datetime import datetime, timezone
from threading import Lock

from sqlalchemy import select

from app.models import ReviewTask
from app.providers.feature_router import FeatureProviderSnapshot
from app.services.inference import InferenceCoordinator
from app.services.review_pipeline import ACTIVE_TASK_STATES, ReviewPipeline
from app.services.audit import AuditService


class ReviewExecutor:
    """Process-local, bounded executor for document review tasks."""

    max_workers = 2

    def __init__(
        self,
        session_factory,
        coordinator: InferenceCoordinator,
    ):
        self._pipeline = ReviewPipeline(
            session_factory,
            coordinator,
        )
        self._pool = ThreadPoolExecutor(
            max_workers=self.max_workers, thread_name_prefix="review"
        )
        self._futures: dict[str, Future] = {}
        self._lock = Lock()
        self._closed = False

    def submit(
        self,
        task_id: str,
        snapshot: FeatureProviderSnapshot,
    ) -> None:
        deferred: Future | None = None
        future: Future | None = None
        with self._lock:
            if self._closed:
                raise RuntimeError("审核执行器已关闭")
            current = self._futures.get(task_id)
            if current is not None and not current.done():
                deferred = current
            else:
                future = self._pool.submit(
                    self._pipeline.run,
                    task_id,
                    snapshot,
                )
                self._futures[task_id] = future
        if deferred is not None:
            deferred.add_done_callback(
                lambda _, key=task_id, provider=snapshot: (
                    self._submit_deferred(key, provider)
                )
            )
            return
        assert future is not None
        future.add_done_callback(
            lambda completed, key=task_id: self._forget(key, completed)
        )

    def cancel(self, task_id: str) -> bool:
        with self._lock:
            future = self._futures.get(task_id)
            return future.cancel() if future is not None else False

    def shutdown(self) -> None:
        with self._lock:
            self._closed = True
        self._pool.shutdown(wait=True, cancel_futures=True)

    def _submit_deferred(
        self,
        task_id: str,
        snapshot: FeatureProviderSnapshot,
    ) -> None:
        with self._lock:
            if self._closed:
                return
        self.submit(task_id, snapshot)

    def _forget(self, task_id: str, completed: Future) -> None:
        with self._lock:
            if self._futures.get(task_id) is completed:
                self._futures.pop(task_id, None)


def interrupt_unfinished_tasks(session, mode: str = "demo") -> int:
    """Mark tasks whose process-local execution was lost across restart."""
    tasks = list(
        session.scalars(
            select(ReviewTask).where(ReviewTask.status.in_(ACTIVE_TASK_STATES))
        )
    )
    now = datetime.now(timezone.utc)
    for task in tasks:
        task.status = "interrupted"
        task.error_summary = "服务重启导致本次审核中断，请重试。"
        task.completed_at = now
    session.commit()
    for task in tasks:
        AuditService(session).record(
            "review.interrupted",
            "interrupted",
            "Review interrupted",
            mode,
            task.id,
        )
    return len(tasks)
