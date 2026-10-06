"""Booking rules. Used by both the regular UI and the AI assistant, so the model can never bypass them."""

from datetime import datetime, timedelta

from django.db import transaction
from django.utils import timezone

from .models import Appointment, Pet, Service, Tutor, Vet, available_slots

LATE_CHANGE_WINDOW = timedelta(hours=24)
LATE_CHANGE_FEE_USD = 25


class BookingError(Exception):
    """A rule was broken. The message is safe to show to the user."""


def vets_for(pet: Pet, service: Service) -> list[Vet]:
    """Vets who see the pet's species and perform the service."""
    return [vet for vet in service.vets.order_by("name") if pet.species in vet.treats]


def own_pet(tutor: Tutor, pet_id: int, lock: bool = False) -> Pet:
    pets = tutor.pets.select_for_update() if lock else tutor.pets
    try:
        return pets.get(id=pet_id)
    except Pet.DoesNotExist:
        raise BookingError("That pet isn't on your account. Call list_my_pets for the right id.") from None


def own_appointment(tutor: Tutor, appointment_id: int, lock: bool = False) -> Appointment:
    """An upcoming, not-yet-completed appointment of this tutor. Past and completed visits are history: they can't
    be moved or cancelled from here."""
    appointments = Appointment.objects.active().exclude(status=Appointment.Status.COMPLETED)
    if lock:
        appointments = appointments.select_for_update(of=("self",))
    try:
        appointment = appointments.select_related("pet", "vet", "service").get(id=appointment_id, pet__tutor=tutor)
    except Appointment.DoesNotExist:
        raise BookingError(
            "That appointment isn't on your account, already happened, or was cancelled. "
            "Call list_my_appointments for the right id."
        ) from None
    if appointment.starts_at <= timezone.now():
        raise BookingError("That appointment has already started or happened, so it can't be changed here.")
    return appointment


def find_slots(pet: Pet, service: Service, day) -> list[tuple[Vet, datetime]]:
    """Every free start time that day, across vets who can see this pet for this service."""
    slots = [(vet, start) for vet in vets_for(pet, service) for start in available_slots(service, vet, day, pet=pet)]
    return sorted(slots, key=lambda s: (s[1], s[0].name))


def check_slot(pet: Pet, vet: Vet, service: Service, starts_at: datetime, ignore: Appointment | None = None):
    if pet.species not in vet.treats:
        raise BookingError(f"{vet} doesn't see {pet.get_species_display().lower()}s.")
    if not vet.services.filter(id=service.id).exists():
        raise BookingError(f"{vet} doesn't do {service.name.lower()}.")
    if starts_at <= timezone.now():
        raise BookingError("That time is in the past.")
    day = timezone.localtime(starts_at).date()
    if starts_at not in available_slots(service, vet, day, pet=pet, ignore=ignore):
        raise BookingError("That time is no longer available (the vet or the pet is already booked).")


@transaction.atomic
def book(tutor: Tutor, pet_id: int, service_id: int, vet_id: int, starts_at: datetime) -> Appointment:
    # Lock the vet and the pet, in that order everywhere, so concurrent bookings for either are serialised.
    vet = Vet.objects.select_for_update().get(id=vet_id)
    pet = own_pet(tutor, pet_id, lock=True)
    service = Service.objects.get(id=service_id)
    check_slot(pet, vet, service, starts_at)
    return Appointment.objects.create(pet=pet, vet=vet, service=service, starts_at=starts_at)


@transaction.atomic
def reschedule(tutor: Tutor, appointment_id: int, starts_at: datetime) -> Appointment:
    appointment = own_appointment(tutor, appointment_id)
    Vet.objects.select_for_update().get(id=appointment.vet_id)
    own_pet(tutor, appointment.pet_id, lock=True)
    appointment = own_appointment(tutor, appointment_id, lock=True)  # re-read under the locks
    check_slot(appointment.pet, appointment.vet, appointment.service, starts_at, ignore=appointment)
    appointment.starts_at = starts_at
    appointment.save(update_fields=["starts_at"])
    return appointment


@transaction.atomic
def cancel(tutor: Tutor, appointment_id: int, reason: str = "") -> Appointment:
    appointment = own_appointment(tutor, appointment_id, lock=True)
    appointment.status = Appointment.Status.CANCELLED
    appointment.cancellation_reason = reason[:255]
    appointment.save(update_fields=["status", "cancellation_reason"])
    return appointment


def late_change_fee(appointment: Appointment) -> int:
    until = appointment.starts_at - timezone.now()
    return LATE_CHANGE_FEE_USD if timedelta(0) < until < LATE_CHANGE_WINDOW else 0
