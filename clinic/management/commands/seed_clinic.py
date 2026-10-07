from django.core.management.base import BaseCommand

from clinic.demo import seed_clinic


class Command(BaseCommand):
    help = "Create the clinic's vets and services if there are none yet (runs on every boot)."

    def add_arguments(self, parser):
        parser.add_argument(
            "--force",
            action="store_true",
            help="Re-sync vets and services from the code, undoing edits made in the admin.",
        )

    def handle(self, *args, **options):
        if seed_clinic(force=options["force"]):
            self.stdout.write(self.style.SUCCESS("Clinic seeded."))
        else:
            self.stdout.write("The clinic already has vets or services; left as is (--force re-syncs from the code).")
