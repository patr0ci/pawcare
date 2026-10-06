from dataclasses import dataclass

from django.conf import settings
from pgvector.django import CosineDistance

from helpcenter.models import Chunk

from .embeddings import get_embedder


@dataclass
class Source:
    number: int
    title: str
    url: str
    text: str
    distance: float


def retrieve(query: str, k: int | None = None, max_distance: float | None = None) -> list[Source]:
    """Top-k chunks closest to the query. Chunks beyond `max_distance` are dropped on purpose:
    an empty result is what lets the assistant say "I don't know" instead of guessing."""
    k = k or settings.RAG_TOP_K
    max_distance = settings.RAG_MAX_DISTANCE if max_distance is None else max_distance
    vector = get_embedder().embed([query])[0]
    chunks = (
        Chunk.objects.select_related("article")
        .annotate(distance=CosineDistance("embedding", vector))
        .filter(distance__lte=max_distance)
        .order_by("distance")[:k]
    )
    # One source per article (chunks merged in article order), ranked by its best chunk,
    # so citations point to pages the user can open rather than to fragments.
    by_article: dict[int, list] = {}
    for chunk in chunks:
        by_article.setdefault(chunk.article_id, []).append(chunk)
    return [
        Source(
            number=i,
            title=group[0].article.title,
            url=group[0].article.get_absolute_url(),
            text="\n\n".join(c.text for c in sorted(group, key=lambda c: c.position)),
            distance=float(group[0].distance),
        )
        for i, group in enumerate(by_article.values(), start=1)
    ]
