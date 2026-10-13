from collections.abc import Callable, Iterator
from dataclasses import dataclass
from threading import Event, Lock
from time import monotonic

from sqlalchemy.orm import Session

from app.providers.feature_router import FeatureProviderSnapshot
from app.retrieval.index import SearchResult
from app.schemas import (
    ContextChunk,
    KnowledgeCitationResponse,
    KnowledgeQueryResponse,
)
from app.services.audit import AuditService
from app.services.inference import (
    InferenceCancellation,
    InferenceCoordinator,
)
from app.services.knowledge_index import KnowledgeIndexService

NO_EVIDENCE_ANSWER = "知识库中暂无可靠依据，请调整关键词或资料范围。"
ANSWER_DISCLAIMER = (
    "模型仅提供配置查询与操作参考，不会直接下发配置；"
    "实际变更必须按既有审批流程由人工执行。"
)


@dataclass(frozen=True)
class KnowledgeStreamEvent:
    event: str
    data: dict


class KnowledgeEventStream(Iterator[KnowledgeStreamEvent]):
    """Adds a thread-safe cancellation path around a sync generator."""

    def __init__(
        self,
        factory: Callable[
            ["KnowledgeEventStream"],
            Iterator[KnowledgeStreamEvent],
        ],
    ):
        self.cancelled = Event()
        self._state_lock = Lock()
        self._running = 0
        self._closed = False
        self._cancel_callback: Callable[[], None] | None = None
        self._iterator = factory(self)

    def __iter__(self) -> "KnowledgeEventStream":
        return self

    def __next__(self) -> KnowledgeStreamEvent:
        with self._state_lock:
            if self._closed:
                raise StopIteration
            self._running += 1
        should_close = False
        try:
            return next(self._iterator)
        finally:
            with self._state_lock:
                self._running -= 1
                should_close = self._closed and self._running == 0
            if should_close:
                self._close_iterator()

    def bind_cancel(self, callback: Callable[[], None]) -> None:
        invoke = False
        with self._state_lock:
            self._cancel_callback = callback
            invoke = self._closed
        if invoke:
            callback()

    def close(self) -> None:
        callback = None
        close_iterator = False
        with self._state_lock:
            if self._closed:
                return
            self._closed = True
            self.cancelled.set()
            callback = self._cancel_callback
            close_iterator = self._running == 0
        if callback is not None:
            callback()
        if close_iterator:
            self._close_iterator()

    def _close_iterator(self) -> None:
        close = getattr(self._iterator, "close", None)
        if close is None:
            return
        try:
            close()
        except (RuntimeError, ValueError):
            return


class _ProviderIteratorFactory:
    """Defers provider creation until coordinator admission."""

    def __init__(
        self,
        factory: Callable[[], Iterator[str]],
    ):
        self._factory = factory
        self._lock = Lock()
        self._cancelled = Event()
        self._upstream: Iterator[str] | None = None

    def __call__(self) -> Iterator[str]:
        upstream = iter(self._factory())
        with self._lock:
            self._upstream = upstream
            cancelled = self._cancelled.is_set()
        if cancelled:
            self._cancel(upstream)
        return upstream

    def cancel(self) -> None:
        self._cancelled.set()
        with self._lock:
            upstream = self._upstream
        if upstream is not None:
            self._cancel(upstream)

    @staticmethod
    def _cancel(upstream: Iterator[str]) -> None:
        cancel = getattr(upstream, "cancel", None)
        if cancel is not None:
            cancel()


class KnowledgeService:
    """Streams text while citations remain server-owned retrieval facts."""

    def __init__(
        self,
        session: Session,
        index_service: KnowledgeIndexService,
        coordinator: InferenceCoordinator,
        feature_snapshot: FeatureProviderSnapshot,
        minimum_score: float = 0.30,
    ):
        self.session = session
        self.index_service = index_service
        self.coordinator = coordinator
        self.feature_snapshot = feature_snapshot
        self.minimum_score = minimum_score

    def stream(
        self, question: str, scope: str | None
    ) -> KnowledgeEventStream:
        return KnowledgeEventStream(
            lambda control: self._stream_events(
                question, scope, control
            )
        )

    def _stream_events(
        self,
        question: str,
        scope: str | None,
        control: KnowledgeEventStream,
    ) -> Iterator[KnowledgeStreamEvent]:
        started_at = monotonic()
        results = [
            result
            for result in self.index_service.search(
                question, scope, limit=4
            )
            if result.score >= self.minimum_score
        ]
        citations = self._citations(results)
        yield KnowledgeStreamEvent(
            "retrieval",
            {
                "citations": [
                    citation.model_dump(mode="json")
                    for citation in citations
                ],
                "retrieval_engine": self.index_service.engine_name,
            },
        )
        if not citations:
            self._audit(
                "knowledge.query.no_evidence",
                "no_evidence",
                None,
            )
            yield KnowledgeStreamEvent(
                "done",
                {
                    "status": "no_evidence",
                    "answer": NO_EVIDENCE_ANSWER,
                    "elapsed_ms": self._elapsed_ms(started_at),
                },
            )
            return

        yield KnowledgeStreamEvent(
            "meta",
            {
                "mode": self.feature_snapshot.mode,
                "model": self.feature_snapshot.model,
            },
        )
        chunks = [
            ContextChunk(
                chunk_id=result.chunk_id,
                source_id=result.source_id,
                text=result.text,
                location=result.location,
            )
            for result in results
        ]
        provider_factory = _ProviderIteratorFactory(
            lambda: (
                self.feature_snapshot.provider
                .stream_answer_with_context(question, chunks)
            )
        )
        cancellation = InferenceCancellation()
        coordinated = self.coordinator.stream(
            "knowledge",
            "knowledge_answer",
            self.feature_snapshot,
            provider_factory,
            cancellation=cancellation,
        )
        control.bind_cancel(
            lambda: (
                cancellation.cancel(),
                provider_factory.cancel(),
            )
        )
        completed = False
        terminal_status = "cancelled"
        try:
            for text in coordinated:
                yield KnowledgeStreamEvent(
                    "delta", {"content": str(text)}
                )
            if control.cancelled.is_set():
                return
            self._audit(
                "knowledge.query.completed",
                "answered",
                results[0].chunk_id,
            )
            completed = True
            yield KnowledgeStreamEvent(
                "done",
                {
                    "status": "answered",
                    "disclaimer": ANSWER_DISCLAIMER,
                    "elapsed_ms": self._elapsed_ms(started_at),
                },
            )
        except GeneratorExit:
            raise
        except BaseException:
            terminal_status = "failed"
            raise
        finally:
            close = getattr(coordinated, "close", None)
            if close is not None:
                close()
            provider_factory.cancel()
            if not completed:
                self._audit(
                    f"knowledge.query.{terminal_status}",
                    terminal_status,
                    results[0].chunk_id,
                )

    def query(
        self, question: str, scope: str | None
    ) -> KnowledgeQueryResponse:
        answer_parts: list[str] = []
        citations: list[KnowledgeCitationResponse] = []
        retrieval_engine = self.index_service.engine_name
        status = "no_evidence"
        for event in self.stream(question, scope):
            if event.event == "retrieval":
                citations = [
                    KnowledgeCitationResponse.model_validate(value)
                    for value in event.data["citations"]
                ]
                retrieval_engine = event.data["retrieval_engine"]
            elif event.event == "delta":
                answer_parts.append(event.data["content"])
            elif event.event == "done":
                status = event.data["status"]
        if status == "no_evidence":
            return KnowledgeQueryResponse(
                status="no_evidence",
                answer=NO_EVIDENCE_ANSWER,
                citations=[],
                retrieval_engine=retrieval_engine,
            )
        return KnowledgeQueryResponse(
            status="answered",
            answer="".join(answer_parts),
            citations=citations,
            retrieval_engine=retrieval_engine,
            disclaimer=ANSWER_DISCLAIMER,
        )

    @staticmethod
    def _citations(
        results: list[SearchResult],
    ) -> list[KnowledgeCitationResponse]:
        return [
            KnowledgeCitationResponse(
                chunk_id=result.chunk_id,
                source_id=result.source_id,
                source_title=result.source_title,
                source_updated_at=result.source_updated_at,
                score=result.score,
                text=result.text,
                location=result.location,
            )
            for result in results[:4]
        ]

    def _audit(
        self,
        event_type: str,
        result_status: str,
        reference_id: str | None,
    ) -> None:
        AuditService(self.session).record(
            event_type=event_type,
            result_status=result_status,
            request_summary="Knowledge query processed",
            reference_id=reference_id,
            mode=self.feature_snapshot.mode,
        )

    @staticmethod
    def _elapsed_ms(started_at: float) -> int:
        return max(0, round((monotonic() - started_at) * 1000))
