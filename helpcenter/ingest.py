"""Load help-center markdown files into Article rows and (re)build their embedded chunks."""

import logging
from pathlib import Path

from django.core.exceptions import ValidationError
from django.db import transaction

from assistant.embeddings import get_embedder

from .chunking import split_into_chunks
from .models import Article, Chunk

CONTENT_DIR = Path(__file__).parent / "content"
logger = logging.getLogger(__name__)


def slug_for(path: Path) -> str:
    """The file name is the article's slug, so it must be a valid /help/<slug>/ URL and fit the column (50 chars).
    Checked up front: an over-long name raises DataError in the database, which would stop the whole ingest."""
    try:
        Article._meta.get_field("slug").clean(path.stem, None)
    except ValidationError as exc:
        raise ValueError(f"{path.name}: the file name isn't a usable slug. {' '.join(exc.messages)}") from exc
    return path.stem


def parse_markdown(path: Path) -> dict:
    text = path.read_text(encoding="utf-8-sig")
    try:
        _, front, body = text.split("---", 2)
        meta = dict(
            line.split(":", 1) for line in front.strip().splitlines() if line.strip() and not line.startswith("#")
        )
        meta["title"], meta["category"]
    except (ValueError, KeyError) as exc:
        raise ValueError(
            f"{path.name}: expected front matter with 'title:' and 'category:' between '---' lines"
        ) from exc
    data = {
        "slug": path.stem,
        "title": meta["title"].strip(),
        "category": meta["category"].strip(),
        "body": body.strip(),
    }
    try:
        Article(**data).clean_fields()  # the slug, and e.g. a title too long for its column (same DataError)
    except ValidationError as exc:
        problems = "; ".join(f"{field}: {' '.join(errors)}" for field, errors in exc.message_dict.items())
        raise ValueError(f"{path.name}: {problems}") from exc
    return data


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


def ingest_directory(directory: Path = CONTENT_DIR) -> dict:
    """Sync the database with the markdown files: new/edited articles are (re)embedded, unchanged ones are
    skipped, and articles whose file is gone are deleted. Safe to run on every deploy."""
    stats = {"created": 0, "updated": 0, "unchanged": 0, "deleted": 0, "chunks": 0, "errors": 0}
    slugs = []
    for path in sorted(directory.glob("*.md")):
        try:
            # Past the slug check, a broken file keeps its existing article instead of deleting it. A file whose
            # name can't be a slug never had a working article: one left by an older ingest would break /help/.
            slugs.append(slug_for(path))
            data = parse_markdown(path)
        except ValueError:
            # One bad file must not stop a deploy (the entrypoint would crash-loop); skip it loudly.
            logger.exception("Skipping help-center file")
            stats["errors"] += 1
            continue
        slug = data.pop("slug")
        article = Article.objects.filter(slug=slug).first()
        if article and all(getattr(article, k) == v for k, v in data.items()) and article.chunks.exists():
            stats["unchanged"] += 1
            continue
        # One transaction: if the new text were saved but its chunks weren't, the next run would find the article
        # "unchanged" and keep answering from the old chunks for good.
        with transaction.atomic():
            article, created = Article.objects.update_or_create(slug=slug, defaults=data)
            stats["chunks"] += index_article(article)
        stats["created" if created else "updated"] += 1
    _, deleted = Article.objects.exclude(slug__in=slugs).delete()  # also cascades their chunks
    stats["deleted"] = deleted.get("helpcenter.Article", 0)
    return stats
