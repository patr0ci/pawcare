from datetime import timedelta

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand
from django.utils import timezone


class Command(BaseCommand):
    help = "Delete throwaway demo accounts (and their pets/appointments) older than N days. Cost history is kept."

    def add_arguments(self, parser):
        parser.add_argument("--days", type=int, default=2)

    def handle(self, *args, **options):
        cutoff = timezone.now() - timedelta(days=options["days"])
        old = get_user_model().objects.filter(username__startswith="demo-", date_joined__lt=cutoff)
        count = old.count()
        old.delete()
        self.stdout.write(self.style.SUCCESS(f"Deleted {count} demo accounts older than {options['days']} days."))
