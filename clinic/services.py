"""Booking rules. Used by both the regular UI and the AI assistant, so the model can never bypass them."""

from datetime import datetime, timedelta

from django.db import transaction
from django.utils import timezone

from .models import Appointment, Pet, Service, Tutor, Vet, available_slots

LATE_CHANGE_WINDOW = timedelta(hours=24)
LATE_CHANGE_FEE_USD = 25


class BookingError(Exception):
    """A rule was broken. The message is safe to show to the user."""


def vets_for(pet: Pet) -> list[Vet]:
    return [vet for vet in Vet.objects.all() if pet.species in vet.treats]


def own_pet(tutor: Tutor, pet_id: int) -> Pet:
    try:
        return tutor.pets.get(id=pet_id)
    except Pet.DoesNotExist:
        raise BookingError("That pet isn't on your account. Call list_my_pets for the right id.") from None


def own_appointment(tutor: Tutor, appointment_id: int) -> Appointment:
    try:
        return Appointment.objects.active().select_related("pet", "vet", "service").get(
            id=appointment_id, pet__tutor=tutor
        )
    except Appointment.DoesNotExist:
        raise BookingError(
            "That appointment isn't on your account or was already cancelled. "
            "Call list_my_appointments for the right id."
        ) from None


def find_slots(pet: Pet, service: Service, day, limit: int = 8) -> list[tuple[Vet, datetime]]:
    slots = [(vet, start) for vet in vets_for(pet) for start in available_slots(service, vet, day)]
    return sorted(slots, key=lambda s: s[1])[:limit]


def _check_slot(pet: Pet, vet: Vet, service: Service, starts_at: datetime, ignore: Appointment | None = None):
    if pet.species not in vet.treats:
        raise BookingError(f"{vet} doesn't see {pet.get_species_display().lower()}s.")
    if starts_at <= timezone.now():
        raise BookingError("That time is in the past.")
    if ignore is not None:
        # Rescheduling: the appointment's own current slot shouldn't block itself.
        ignore.status = Appointment.Status.CANCELLED
        ignore.save(update_fields=["status"])
    if starts_at not in available_slots(service, vet, timezone.localtime(starts_at).date()):
        raise BookingError("That time is no longer available.")


@transaction.atomic
def book(tutor: Tutor, pet_id: int, service_id: int, vet_id: int, starts_at: datetime) -> Appointment:
    pet = own_pet(tutor, pet_id)
    service = Service.objects.get(id=service_id)
    vet = Vet.objects.select_for_update().get(id=vet_id)  # serialises concurrent bookings per vet
    _check_slot(pet, vet, service, starts_at)
    return Appointment.objects.create(pet=pet, vet=vet, service=service, starts_at=starts_at)


@transaction.atomic
def reschedule(tutor: Tutor, appointment_id: int, starts_at: datetime) -> Appointment:
    appointment = own_appointment(tutor, appointment_id)
    Vet.objects.select_for_update().get(id=appointment.vet_id)
    original_status = appointment.status
    _check_slot(appointment.pet, appointment.vet, appointment.service, starts_at, ignore=appointment)
    appointment.starts_at = starts_at
    appointment.status = original_status
    appointment.save(update_fields=["starts_at", "status"])
    return appointment


@transaction.atomic
def cancel(tutor: Tutor, appointment_id: int, reason: str = "") -> Appointment:
    appointment = own_appointment(tutor, appointment_id)
    appointment.status = Appointment.Status.CANCELLED
    appointment.cancellation_reason = reason[:255]
    appointment.save(update_fields=["status", "cancellation_reason"])
    return appointment


def late_change_fee(appointment: Appointment) -> int:
    return LATE_CHANGE_FEE_USD if appointment.starts_at - timezone.now() < LATE_CHANGE_WINDOW else 0
