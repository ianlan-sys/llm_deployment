import numpy as np


class NumpyVectorIndex:
    """Always-available in-process cosine-similarity vector index."""

    engine_name = "numpy"

    def __init__(self) -> None:
        self._vectors = np.empty((0, 0), dtype=np.float32)

    def build(self, vectors: np.ndarray) -> None:
        self._vectors = np.asarray(vectors, dtype=np.float32)

    def search(self, query_vector: np.ndarray, limit: int) -> list[tuple[int, float]]:
        if limit <= 0 or not len(self._vectors):
            return []
        scores = self._vectors @ np.asarray(query_vector, dtype=np.float32)
        indexes = np.argsort(-scores, kind="stable")[:limit]
        return [(int(index), float(scores[index])) for index in indexes]
