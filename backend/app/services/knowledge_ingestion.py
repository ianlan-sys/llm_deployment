from concurrent.futures import Future, ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from threading import Lock
from uuid import NAMESPACE_URL, uuid5

from sqlalchemy import and_, delete, func, or_, select, update

from app.models import AuditEvent, KnowledgeChunk, KnowledgeSource
from app.services.audit import AuditService
from app.services.knowledge_index import KnowledgeIndexService
from app.services.review_parser import parse_document


PARSE_ERROR_SUMMARY = "文档解析失败，请检查文件格式或内容后重试。"
INDEX_ERROR_SUMMARY = "资料索引失败，请重试审核。"
ALLOWED_TRANSITIONS = {
    "uploaded": {"parsing"},
    "parsing": {"pending_review", "parse_failed"},
    "pending_review": {"indexing", "rejected"},
    "indexing": {"active", "index_failed"},
    "parse_failed": {"parsing", "archived"},
    "index_failed": {"indexing", "archived"},
    "active": {"archived"},
    "rejected": {"archived"},
}


class KnowledgeDocumentNotFound(LookupError):
    pass


class InvalidKnowledgeTransition(RuntimeError):
    pass


class KnowledgeIndexingFailed(RuntimeError):
    pass


class KnowledgeIngestionService:
    """Parses uploads and applies database-backed review transitions."""

    def __init__(
        self,
        session_factory,
        mode: str = "mock",
        index_service: KnowledgeIndexService | None = None,
    ):
        self.session_factory = session_factory
        self.mode = mode
        self.index_service = index_service or KnowledgeIndexService()

    def parse(self, document_id: str) -> bool:
        with self.session_factory() as session:
            source = session.get(KnowledgeSource, document_id)
            if source is None:
                raise KnowledgeDocumentNotFound(document_id)
            result = session.execute(
                update(KnowledgeSource)
                .where(
                    KnowledgeSource.id == document_id,
                    KnowledgeSource.status.in_(("uploaded", "parse_failed")),
                )
                .values(
                    status="parsing",
                    error_summary=None,
                    updated_at=_now(),
                )
            )
            if result.rowcount != 1:
                session.rollback()
                return False
            file_path = source.file_path
            media_type = source.media_type
            session.commit()

        try:
            if not file_path or not media_type:
                raise ValueError("missing upload metadata")
            parsed = parse_document(Path(file_path), media_type)
        except Exception:
            return self._mark_parse_failed(document_id)

        sections = parsed.sections
        content = "\n\n".join(section.content for section in sections)
        with self.session_factory() as session:
            try:
                result = session.execute(
                    update(KnowledgeSource)
                    .where(
                        KnowledgeSource.id == document_id,
                        KnowledgeSource.status == "parsing",
                    )
                    .values(
                        content=content,
                        content_summary=content[:240],
                        parse_warnings=[],
                        error_summary=None,
                        status="pending_review",
                        updated_at=_now(),
                    )
                )
                if result.rowcount != 1:
                    session.rollback()
                    return False
                session.execute(
                    delete(KnowledgeChunk).where(
                        KnowledgeChunk.source_id == document_id
                    )
                )
                session.add_all(
                    KnowledgeChunk(
                        id=_chunk_id(document_id, position),
                        source_id=document_id,
                        content=section.content,
                        location=section.title,
                        position=position,
                        index_key=f"upload-{document_id}-{position}",
                    )
                    for position, section in enumerate(sections, start=1)
                )
                self._add_audit(
                    session,
                    document_id,
                    "knowledge_document.parsed",
                    "pending_review",
                    "Knowledge document parsed",
                )
                session.commit()
            except Exception:
                session.rollback()
                raise
        return True

    def approve(self, document_id: str, reviewer: str) -> None:
        with self.index_service.mutation():
            with self.session_factory() as session:
                try:
                    if session.get(KnowledgeSource, document_id) is None:
                        raise KnowledgeDocumentNotFound(document_id)
                    chunk_count = session.scalar(
                        select(func.count())
                        .select_from(KnowledgeChunk)
                        .where(KnowledgeChunk.source_id == document_id)
                    )
                    if not chunk_count:
                        raise InvalidKnowledgeTransition(
                            "没有可审核的解析片段"
                        )
                    started = session.execute(
                        update(KnowledgeSource)
                        .where(
                            KnowledgeSource.id == document_id,
                            KnowledgeSource.status.in_(
                                ("pending_review", "index_failed")
                            ),
                        )
                        .values(
                            status="indexing",
                            error_summary=None,
                            updated_at=_now(),
                        )
                    )
                    if started.rowcount != 1:
                        raise InvalidKnowledgeTransition(
                            "资料当前状态不可审核通过"
                        )
                    session.flush()
                    try:
                        candidate = self.index_service.build_candidate(
                            session,
                            include_document_id=document_id,
                        )
                    except Exception as exc:
                        session.rollback()
                        self._mark_index_failed(document_id)
                        raise KnowledgeIndexingFailed(
                            INDEX_ERROR_SUMMARY
                        ) from exc

                    activated_at = _now()
                    finished = session.execute(
                        update(KnowledgeSource)
                        .where(
                            KnowledgeSource.id == document_id,
                            KnowledgeSource.status == "indexing",
                        )
                        .values(
                            status="active",
                            reviewed_at=activated_at,
                            reviewed_by=reviewer,
                            updated_at=activated_at,
                        )
                    )
                    if finished.rowcount != 1:
                        raise InvalidKnowledgeTransition(
                            "资料状态已变化，请刷新后重试"
                        )
                    self._add_audit(
                        session,
                        document_id,
                        "knowledge_document.approved",
                        "active",
                        "Knowledge document approved",
                    )
                    try:
                        self.index_service.activate(
                            candidate, session.commit
                        )
                    except Exception as exc:
                        session.rollback()
                        self._mark_index_failed(
                            document_id,
                            activated_by=reviewer,
                            activated_at=activated_at,
                            remove_approved=True,
                        )
                        raise KnowledgeIndexingFailed(
                            INDEX_ERROR_SUMMARY
                        ) from exc
                except (
                    KnowledgeDocumentNotFound,
                    InvalidKnowledgeTransition,
                    KnowledgeIndexingFailed,
                ):
                    session.rollback()
                    raise
                except Exception:
                    session.rollback()
                    raise

    def reject(self, document_id: str, reviewer: str) -> None:
        self._review_transition(
            document_id,
            expected=("pending_review",),
            target="rejected",
            reviewer=reviewer,
            event_type="knowledge_document.rejected",
            summary="Knowledge document rejected",
        )

    def archive(self, document_id: str, reviewer: str) -> None:
        with self.index_service.mutation():
            with self.session_factory() as session:
                try:
                    if session.get(KnowledgeSource, document_id) is None:
                        raise KnowledgeDocumentNotFound(document_id)
                    candidate = self.index_service.build_candidate(
                        session,
                        exclude_document_id=document_id,
                    )
                    archived_at = _now()
                    result = session.execute(
                        update(KnowledgeSource)
                        .where(
                            KnowledgeSource.id == document_id,
                            KnowledgeSource.status.in_(
                                (
                                    "parse_failed",
                                    "index_failed",
                                    "active",
                                    "rejected",
                                )
                            ),
                        )
                        .values(
                            status="archived",
                            reviewed_at=archived_at,
                            reviewed_by=reviewer,
                            updated_at=archived_at,
                        )
                    )
                    if result.rowcount != 1:
                        raise InvalidKnowledgeTransition(
                            "资料当前状态不允许此操作"
                        )
                    self._add_audit(
                        session,
                        document_id,
                        "knowledge_document.archived",
                        "archived",
                        "Knowledge document archived",
                    )
                    try:
                        self.index_service.activate(
                            candidate, session.commit
                        )
                    except Exception:
                        session.rollback()
                        if not self._archive_commit_landed(
                            document_id,
                            reviewer,
                            archived_at,
                        ):
                            raise
                        self.index_service.activate(
                            candidate, lambda: None
                        )
                except Exception:
                    session.rollback()
                    raise

    def _review_transition(
        self,
        document_id: str,
        *,
        expected: tuple[str, ...],
        target: str,
        reviewer: str,
        event_type: str,
        summary: str,
    ) -> None:
        with self.session_factory() as session:
            try:
                if session.get(KnowledgeSource, document_id) is None:
                    raise KnowledgeDocumentNotFound(document_id)
                result = session.execute(
                    update(KnowledgeSource)
                    .where(
                        KnowledgeSource.id == document_id,
                        KnowledgeSource.status.in_(expected),
                    )
                    .values(
                        status=target,
                        reviewed_at=_now(),
                        reviewed_by=reviewer,
                        updated_at=_now(),
                    )
                )
                if result.rowcount != 1:
                    raise InvalidKnowledgeTransition(
                        "资料当前状态不允许此操作"
                    )
                self._add_audit(
                    session,
                    document_id,
                    event_type,
                    target,
                    summary,
                )
                session.commit()
            except Exception:
                session.rollback()
                raise

    def _archive_commit_landed(
        self,
        document_id: str,
        reviewer: str,
        archived_at: datetime,
    ) -> bool:
        with self.session_factory() as session:
            return (
                session.scalar(
                    select(KnowledgeSource.id).where(
                        KnowledgeSource.id == document_id,
                        KnowledgeSource.status == "archived",
                        KnowledgeSource.reviewed_by == reviewer,
                        KnowledgeSource.reviewed_at == archived_at,
                    )
                )
                is not None
            )

    def _mark_parse_failed(self, document_id: str) -> bool:
        return self._mark_failed_if_status(
            document_id,
            expected=("parsing",),
        )

    def _mark_index_failed(
        self,
        document_id: str,
        *,
        activated_by: str | None = None,
        activated_at: datetime | None = None,
        remove_approved: bool = False,
    ) -> None:
        with self.session_factory() as session:
            try:
                expected_status = KnowledgeSource.status.in_(
                    (
                        "pending_review",
                        "indexing",
                        "index_failed",
                    )
                )
                if activated_by is not None and activated_at is not None:
                    expected_status = or_(
                        expected_status,
                        and_(
                            KnowledgeSource.status == "active",
                            KnowledgeSource.reviewed_by == activated_by,
                            KnowledgeSource.reviewed_at == activated_at,
                        ),
                    )
                result = session.execute(
                    update(KnowledgeSource)
                    .where(
                        KnowledgeSource.id == document_id,
                        expected_status,
                    )
                    .values(
                        status="index_failed",
                        error_summary=INDEX_ERROR_SUMMARY,
                        updated_at=_now(),
                    )
                )
                if result.rowcount != 1:
                    raise InvalidKnowledgeTransition(
                        "资料状态已变化，请刷新后重试"
                    )
                if remove_approved:
                    session.execute(
                        delete(AuditEvent).where(
                            AuditEvent.reference_id == document_id,
                            AuditEvent.event_type
                            == "knowledge_document.approved",
                            AuditEvent.result_status == "active",
                        )
                    )
                self._add_audit(
                    session,
                    document_id,
                    "knowledge_document.failed",
                    "index_failed",
                    "Knowledge document indexing failed",
                )
                session.commit()
            except Exception:
                session.rollback()
                raise

    def recover_cancelled(self, document_id: str) -> bool:
        """Recover an accepted upload whose queued parse never started."""
        return self._mark_failed_if_status(
            document_id,
            expected=("uploaded",),
        )

    def recover_interrupted(self) -> int:
        """Recover process-local parse states left behind by a prior process."""
        with self.session_factory() as session:
            try:
                candidates = list(
                    session.execute(
                        select(KnowledgeSource.id, KnowledgeSource.status).where(
                            KnowledgeSource.status.in_(("uploaded", "parsing"))
                        )
                    )
                )
                recovered = 0
                for document_id, expected_status in candidates:
                    result = session.execute(
                        update(KnowledgeSource)
                        .where(
                            KnowledgeSource.id == document_id,
                            KnowledgeSource.status == expected_status,
                        )
                        .values(
                            status="parse_failed",
                            error_summary=PARSE_ERROR_SUMMARY,
                            updated_at=_now(),
                        )
                    )
                    if result.rowcount != 1:
                        continue
                    self._add_audit(
                        session,
                        document_id,
                        "knowledge_document.failed",
                        "parse_failed",
                        "Knowledge document failed",
                    )
                    recovered += 1
                session.commit()
                return recovered
            except Exception:
                session.rollback()
                raise

    def _mark_failed_if_status(
        self,
        document_id: str,
        *,
        expected: tuple[str, ...],
    ) -> bool:
        with self.session_factory() as session:
            try:
                result = session.execute(
                    update(KnowledgeSource)
                    .where(
                        KnowledgeSource.id == document_id,
                        KnowledgeSource.status.in_(expected),
                    )
                    .values(
                        status="parse_failed",
                        error_summary=PARSE_ERROR_SUMMARY,
                        updated_at=_now(),
                    )
                )
                if result.rowcount != 1:
                    session.rollback()
                    return False
                self._add_audit(
                    session,
                    document_id,
                    "knowledge_document.failed",
                    "parse_failed",
                    "Knowledge document failed",
                )
                session.commit()
                return True
            except Exception:
                session.rollback()
                raise

    def _add_audit(
        self,
        session,
        document_id: str,
        event_type: str,
        result_status: str,
        summary: str,
    ) -> None:
        AuditService(session).add(
            event_type,
            result_status,
            summary,
            self.mode,
            document_id,
        )


class KnowledgeIngestionExecutor:
    max_workers = 2

    def __init__(self, service: KnowledgeIngestionService):
        self.service = service
        self._pool = ThreadPoolExecutor(
            max_workers=self.max_workers,
            thread_name_prefix="knowledge-parse",
        )
        self._futures: dict[str, Future] = {}
        self._lock = Lock()
        self._closed = False

    def submit(self, document_id: str) -> None:
        with self._lock:
            if self._closed:
                raise RuntimeError("知识解析执行器已关闭")
            current = self._futures.get(document_id)
            if current is not None and not current.done():
                raise InvalidKnowledgeTransition("资料解析已在进行")
            future = self._pool.submit(self.service.parse, document_id)
            self._futures[document_id] = future
        future.add_done_callback(
            lambda completed, key=document_id: self._forget(key, completed)
        )

    def shutdown(self) -> None:
        with self._lock:
            self._closed = True
            futures = list(self._futures.items())
        error: BaseException | None = None
        for document_id, future in futures:
            if not future.cancel():
                continue
            try:
                self.service.recover_cancelled(document_id)
            except BaseException as exc:
                if error is None:
                    error = exc
        self._pool.shutdown(wait=True, cancel_futures=False)
        if error is not None:
            raise error

    def _forget(self, document_id: str, completed: Future) -> None:
        with self._lock:
            if self._futures.get(document_id) is completed:
                self._futures.pop(document_id, None)


def _chunk_id(document_id: str, position: int) -> str:
    return str(uuid5(NAMESPACE_URL, f"knowledge-upload/{document_id}/{position}"))


def _now() -> datetime:
    return datetime.now(timezone.utc)
