from django.core.management.base import BaseCommand

from helpcenter.ingest import ingest_directory
from helpcenter.models import Article


class Command(BaseCommand):
    help = "Load helpcenter/content/*.md into the database and rebuild the embedding index."

    def add_arguments(self, parser):
        parser.add_argument("--if-empty", action="store_true", help="Skip if articles are already indexed.")

    def handle(self, *args, **options):
        if options["if_empty"] and Article.objects.exists():
            self.stdout.write("Help center already indexed; skipping.")
            return
        articles, chunks = ingest_directory()
        self.stdout.write(self.style.SUCCESS(f"Indexed {articles} articles into {chunks} chunks."))
