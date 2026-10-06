from django.core.management.base import BaseCommand

from clinic.demo import seed_clinic


class Command(BaseCommand):
    help = "Create the clinic's vets and services (idempotent)."

    def handle(self, *args, **options):
        seed_clinic()
        self.stdout.write(self.style.SUCCESS("Clinic seeded."))
