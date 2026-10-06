"""Text embeddings. Local by default (fastembed, no API key or per-call cost); a hashing fake for tests."""

import hashlib
import math
import re
from functools import lru_cache

from django.conf import settings


class FakeEmbedder:
    """Deterministic bag-of-words hashing. Good enough for tests to exercise retrieval for real."""

    def __init__(self, dim: int):
        self.dim = dim

    def embed(self, texts: list[str]) -> list[list[float]]:
        return [self._one(t) for t in texts]

    def _one(self, text: str) -> list[float]:
        vec = [0.0] * self.dim
        for word in re.findall(r"[a-z0-9]+", text.lower()):
            bucket = int(hashlib.md5(word.encode()).hexdigest(), 16) % self.dim
            vec[bucket] += 1.0
        norm = math.sqrt(sum(v * v for v in vec)) or 1.0
        return [v / norm for v in vec]


class FastEmbedder:
    def __init__(self, model_name: str, cache_dir: str):
        from fastembed import TextEmbedding

        self.model = TextEmbedding(model_name, cache_dir=cache_dir)

    def embed(self, texts: list[str]) -> list[list[float]]:
        return [vector.tolist() for vector in self.model.embed(texts)]


@lru_cache(maxsize=1)
def get_embedder():
    if settings.EMBEDDING_PROVIDER == "fake":
        return FakeEmbedder(settings.EMBEDDING_DIM)
    return FastEmbedder(settings.EMBEDDING_MODEL, settings.EMBEDDING_CACHE_DIR)
