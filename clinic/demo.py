"""Seed data and throwaway demo accounts, so every visitor gets their own pets to book for."""

import secrets
from datetime import date
from decimal import Decimal

from django.contrib.auth import get_user_model

from .models import Pet, Service, Tutor, Vet

VETS = [
    ("Dr. Maya Chen", "General practice", ["dog", "cat"]),
    ("Dr. Rafael Souza", "Surgery", ["dog", "cat"]),
    ("Dr. Lena Ortiz", "Exotics", ["rabbit", "bird", "cat"]),
]

GENERAL = ["Wellness exam", "Vaccination visit", "Nail trim"]
SURGICAL = ["Dental cleaning", "Spay/neuter (cat)", "Spay/neuter (dog under 20 kg)", "Spay/neuter (dog 20 kg+)"]
VET_SERVICES = {
    "Dr. Maya Chen": GENERAL,
    "Dr. Rafael Souza": SURGICAL,
    "Dr. Lena Ortiz": GENERAL,
}

SERVICES = [
    ("Wellness exam", 30, "65.00"),
    ("Vaccination visit", 30, "45.00"),
    ("Dental cleaning", 90, "320.00"),
    ("Spay/neuter (cat)", 60, "280.00"),
    ("Spay/neuter (dog under 20 kg)", 60, "380.00"),
    ("Spay/neuter (dog 20 kg+)", 60, "460.00"),
    ("Nail trim", 15, "20.00"),
]


def seed_clinic() -> None:
    for name, specialty, treats in VETS:
        Vet.objects.update_or_create(name=name, defaults={"specialty": specialty, "treats": treats})
    for name, minutes, price in SERVICES:
        Service.objects.update_or_create(
            name=name, defaults={"duration_minutes": minutes, "price_usd": Decimal(price)}
        )
    for vet_name, service_names in VET_SERVICES.items():
        Vet.objects.get(name=vet_name).services.set(Service.objects.filter(name__in=service_names))


def create_demo_tutor() -> Tutor:
    user = get_user_model().objects.create_user(
        username=f"demo-{secrets.token_hex(4)}",
        password=None,
        first_name="Demo",
        last_name="Visitor",
    )
    tutor = Tutor.objects.create(user=user, phone="(555) 010-0000")
    Pet.objects.create(
        tutor=tutor, name="Biscuit", species=Pet.Species.DOG, breed="Beagle",
        birth_date=date(2021, 4, 12), weight_kg=Decimal("11.4"),
    )
    Pet.objects.create(
        tutor=tutor, name="Miso", species=Pet.Species.CAT, breed="Domestic shorthair",
        birth_date=date(2023, 9, 2), weight_kg=Decimal("4.1"), allergies="Chicken",
    )
    return tutor
