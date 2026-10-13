from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

import numpy as np


@dataclass(frozen=True)
class SearchResult:
    chunk_id: str
    score: float
    source_id: str
    source_title: str
    source_updated_at: datetime
    text: str
    location: str


class VectorIndex(Protocol):
    engine_name: str

    def build(self, vectors: np.ndarray) -> None: ...

    def search(self, query_vector: np.ndarray, limit: int) -> list[tuple[int, float]]: ...
