import numpy as np


class FaissVectorIndex:
    """Optional FAISS index; importing this module never requires FAISS."""

    engine_name = "faiss"

    def __init__(self) -> None:
        try:
            import faiss  # type: ignore[import-not-found]
        except ImportError as exc:
            raise ImportError("FAISS is not installed") from exc
        self._faiss = faiss
        self._index = None

    def build(self, vectors: np.ndarray) -> None:
        matrix = np.ascontiguousarray(vectors, dtype=np.float32)
        self._index = self._faiss.IndexFlatIP(matrix.shape[1])
        self._index.add(matrix)

    def search(self, query_vector: np.ndarray, limit: int) -> list[tuple[int, float]]:
        if self._index is None or limit <= 0:
            return []
        scores, indexes = self._index.search(
            np.ascontiguousarray(query_vector, dtype=np.float32).reshape(1, -1), limit
        )
        return [
            (int(index), float(score))
            for index, score in zip(indexes[0], scores[0], strict=True)
            if index >= 0
        ]
