"""Load help-center markdown files into Article rows and (re)build their embedded chunks."""

from pathlib import Path

from django.db import transaction

from assistant.embeddings import get_embedder

from .chunking import split_into_chunks
from .models import Article, Chunk

CONTENT_DIR = Path(__file__).parent / "content"


def parse_markdown(path: Path) -> dict:
    text = path.read_text(encoding="utf-8")
    _, front, body = text.split("---", 2)
    meta = dict(line.split(":", 1) for line in front.strip().splitlines())
    return {
        "slug": path.stem,
        "title": meta["title"].strip(),
        "category": meta["category"].strip(),
        "body": body.strip(),
    }


@transaction.atomic
def index_article(article: Article) -> int:
    texts = split_into_chunks(article.title, article.body)
    vectors = get_embedder().embed(texts)
    article.chunks.all().delete()
    Chunk.objects.bulk_create(
        Chunk(article=article, position=i, text=text, embedding=vector)
        for i, (text, vector) in enumerate(zip(texts, vectors, strict=True))
    )
    return len(texts)


def ingest_directory(directory: Path = CONTENT_DIR) -> tuple[int, int]:
    articles = chunks = 0
    for path in sorted(directory.glob("*.md")):
        data = parse_markdown(path)
        article, _ = Article.objects.update_or_create(slug=data.pop("slug"), defaults=data)
        chunks += index_article(article)
        articles += 1
    return articles, chunks
