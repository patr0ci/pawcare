from datetime import timedelta

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand
from django.utils import timezone

from clinic.models import Appointment


class Command(BaseCommand):
    help = (
        "Delete throwaway demo accounts (and their pets/appointments) older than N days, and appointments that demo "
        "accounts booked more than N hours ago. Cost history is kept."
    )

    def add_arguments(self, parser):
        parser.add_argument("--days", type=int, default=2)
        # Every visitor shares the vets' calendar: without this, a busy day of demo bookings fills the slots
        # the suggested "book a wellness exam next Tuesday" question needs.
        parser.add_argument("--appointment-hours", type=int, default=2)

    def handle(self, *args, **options):
        now = timezone.now()
        booked = Appointment.objects.filter(
            pet__tutor__user__username__startswith="demo-",
            created_at__lt=now - timedelta(hours=options["appointment_hours"]),
        )
        appointments, _ = booked.delete()
        old = get_user_model().objects.filter(username__startswith="demo-", date_joined__lt=now - timedelta(days=options["days"]))
        count = old.count()
        old.delete()
        self.stdout.write(
            self.style.SUCCESS(
                f"Deleted {count} demo accounts older than {options['days']} days and {appointments} demo appointments "
                f"booked more than {options['appointment_hours']} hours ago."
            )
        )
