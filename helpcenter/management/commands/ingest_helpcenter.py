from django.core.management.base import BaseCommand

from helpcenter.ingest import ingest_directory


class Command(BaseCommand):
    help = "Sync helpcenter/content/*.md into the database, re-embedding only new or edited articles."

    def handle(self, *args, **options):
        stats = ingest_directory()
        self.stdout.write(self.style.SUCCESS(", ".join(f"{k}: {v}" for k, v in stats.items())))
