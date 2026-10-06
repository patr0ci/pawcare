from django.core.management.base import BaseCommand

from helpcenter.ingest import ingest_directory


class Command(BaseCommand):
    help = "Load helpcenter/content/*.md into the database and rebuild the embedding index."

    def handle(self, *args, **options):
        articles, chunks = ingest_directory()
        self.stdout.write(self.style.SUCCESS(f"Indexed {articles} articles into {chunks} chunks."))
