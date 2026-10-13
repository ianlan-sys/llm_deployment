from sqlalchemy.orm import Session

from app.retrieval.faiss_index import FaissVectorIndex
from app.retrieval.index import SearchResult, VectorIndex
from app.retrieval.numpy_index import NumpyVectorIndex
from app.services.knowledge_index import KnowledgeIndexService


class RetrievalService:
    """Retrieves active knowledge chunks with a deterministic local fallback."""

    def __init__(self, session: Session, index: VectorIndex | None = None):
        self.session = session
        self.index = index or self._default_index()
        self._knowledge_index = KnowledgeIndexService()
        self._knowledge_index.rebuild(session)

    @property
    def engine_name(self) -> str:
        return self._knowledge_index.engine_name

    def search(
        self, query: str, scope: str | None, limit: int = 4
    ) -> list[SearchResult]:
        return self._knowledge_index.search(query, scope, limit)

    @staticmethod
    def _default_index() -> VectorIndex:
        try:
            return FaissVectorIndex()
        except ImportError:
            return NumpyVectorIndex()
