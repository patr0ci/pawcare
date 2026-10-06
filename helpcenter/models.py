from django.conf import settings
from django.db import models
from django.urls import reverse
from pgvector.django import VectorField


class Article(models.Model):
    slug = models.SlugField(unique=True)
    title = models.CharField(max_length=200)
    category = models.CharField(max_length=60)
    body = models.TextField(help_text="Markdown-ish plain text. Paragraphs are separated by blank lines.")
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["category", "title"]

    def __str__(self):
        return self.title

    def get_absolute_url(self):
        return reverse("helpcenter:article", args=[self.slug])


class Chunk(models.Model):
    """A retrieval unit: a few paragraphs of one article plus its embedding."""

    article = models.ForeignKey(Article, on_delete=models.CASCADE, related_name="chunks")
    position = models.PositiveIntegerField()
    text = models.TextField()
    embedding = VectorField(dimensions=settings.EMBEDDING_DIM)

    class Meta:
        ordering = ["article", "position"]
        # No ANN (HNSW) index on purpose: a help center is a few hundred chunks, where an exact scan takes
        # milliseconds and is always correct. With HNSW, rows deleted by a re-ingest (not yet vacuumed) can fill
        # the candidate list and make a search come back empty. Add one past ~10k chunks, with iterative scans.

    def __str__(self):
        return f"{self.article.slug}#{self.position}"
