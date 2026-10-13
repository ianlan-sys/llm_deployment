from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from threading import RLock

import numpy as np
from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.models import KnowledgeChunk, KnowledgeSource
from app.retrieval.hashing import HASH_DIMENSIONS, hash_text, tokenize
from app.retrieval.index import SearchResult


@dataclass(frozen=True)
class IndexedChunk:
    chunk_id: str
    source_id: str
    source_title: str
    source_updated_at: datetime
    scope: str
    text: str
    location: str


@dataclass(frozen=True)
class KnowledgeIndexSnapshot:
    records: tuple[IndexedChunk, ...]
    vectors: np.ndarray
    engine_name: str
    built_at: datetime


class KnowledgeIndexService:
    """Publishes immutable active-document snapshots for lock-light search."""

    def __init__(self):
        vectors = _freeze_vectors(
            np.empty((0, HASH_DIMENSIONS), dtype=np.float32)
        )
        self._snapshot = KnowledgeIndexSnapshot(
            records=(),
            vectors=vectors,
            engine_name="numpy",
            built_at=datetime.now(timezone.utc),
        )
        self._lock = RLock()
        self._mutation_lock = RLock()

    def rebuild(self, session: Session) -> KnowledgeIndexSnapshot:
        with self.mutation():
            candidate = self.build_candidate(session)
            return self.activate(candidate, lambda: None)

    def build_candidate(
        self,
        session: Session,
        *,
        include_document_id: str | None = None,
        exclude_document_id: str | None = None,
    ) -> KnowledgeIndexSnapshot:
        """Build vectors and their metadata together without the publish lock."""
        active_filter = KnowledgeSource.status == "active"
        if include_document_id is not None:
            active_filter = or_(
                active_filter,
                KnowledgeSource.id == include_document_id,
            )
        statement = (
            select(KnowledgeChunk, KnowledgeSource)
            .join(KnowledgeChunk.source)
            .where(active_filter)
            .order_by(
                KnowledgeSource.id,
                KnowledgeChunk.position,
                KnowledgeChunk.id,
            )
        )
        if exclude_document_id is not None:
            statement = statement.where(
                KnowledgeSource.id != exclude_document_id
            )
        rows = list(session.execute(statement))
        records = tuple(
            IndexedChunk(
                chunk_id=chunk.id,
                source_id=source.id,
                source_title=source.title,
                source_updated_at=_as_utc(source.updated_at),
                scope=source.scope,
                text=chunk.content,
                location=chunk.location,
            )
            for chunk, source in rows
        )
        if records:
            vectors = np.vstack(
                [hash_text(record.text) for record in records]
            ).astype(np.float32, copy=False)
        else:
            vectors = np.empty(
                (0, HASH_DIMENSIONS), dtype=np.float32
            )
        vectors = _freeze_vectors(vectors)
        return KnowledgeIndexSnapshot(
            records=records,
            vectors=vectors,
            engine_name="numpy",
            built_at=datetime.now(timezone.utc),
        )

    def search(
        self, query: str, scope: str | None, limit: int = 4
    ) -> list[SearchResult]:
        if limit <= 0 or not query.strip():
            return []
        snapshot = self._acquire_snapshot()
        eligible = [
            position
            for position, record in enumerate(snapshot.records)
            if scope is None or record.scope == scope
        ]
        if not eligible:
            return []

        query_vector = hash_text(query)
        query_tokens = tokenize(query)
        ranked = []
        for position in eligible:
            record = snapshot.records[position]
            cosine = max(
                float(snapshot.vectors[position] @ query_vector),
                0.0,
            )
            chunk_tokens = tokenize(record.text)
            overlap = len(query_tokens & chunk_tokens) / max(
                len(query_tokens), 1
            )
            ranked.append(
                SearchResult(
                    chunk_id=record.chunk_id,
                    score=0.75 * cosine + 0.25 * overlap,
                    source_id=record.source_id,
                    source_title=record.source_title,
                    source_updated_at=record.source_updated_at,
                    text=record.text,
                    location=record.location,
                )
            )
        maximum = min(limit, 4)
        return sorted(
            ranked,
            key=lambda result: (-result.score, result.chunk_id),
        )[:maximum]

    def activate(
        self,
        candidate: KnowledgeIndexSnapshot,
        commit: Callable[[], None],
    ) -> KnowledgeIndexSnapshot:
        with self._lock:
            commit()
            self._snapshot = candidate
            return candidate

    def _acquire_snapshot(self) -> KnowledgeIndexSnapshot:
        with self._lock:
            return self._snapshot

    @property
    def engine_name(self) -> str:
        return self._acquire_snapshot().engine_name

    @contextmanager
    def mutation(self) -> Iterator[None]:
        """Serialize candidate builds without blocking searches."""
        with self._mutation_lock:
            yield


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _freeze_vectors(vectors: np.ndarray) -> np.ndarray:
    contiguous = np.ascontiguousarray(vectors, dtype=np.float32)
    return np.frombuffer(
        contiguous.tobytes(), dtype=np.float32
    ).reshape(contiguous.shape)
