import hashlib
import re

import numpy as np

HASH_DIMENSIONS = 384
_TOKEN_PATTERN = re.compile(r"[a-z0-9_.:-]+|[\u4e00-\u9fff]", re.IGNORECASE)


def tokenize(text: str) -> set[str]:
    """Return deterministic Latin-word and individual CJK-character features."""
    return set(_TOKEN_PATTERN.findall(text.lower()))


def hash_text(text: str, dimensions: int = HASH_DIMENSIONS) -> np.ndarray:
    """Map text to a stable, L2-normalized feature-hashing vector."""
    if dimensions <= 0:
        raise ValueError("dimensions must be greater than zero")
    vector = np.zeros(dimensions, dtype=np.float32)
    for token in tokenize(text):
        digest = hashlib.sha256(token.encode("utf-8")).digest()
        feature = int.from_bytes(digest[:8], "big") % dimensions
        vector[feature] += -1.0 if digest[8] & 1 else 1.0
    norm = float(np.linalg.norm(vector))
    if norm:
        vector /= norm
    return vector
