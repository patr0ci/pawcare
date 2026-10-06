from datetime import date, datetime, timedelta

import pytest
from django.utils import timezone

from clinic.models import Appointment, Service, Vet, available_slots


def next_weekday(weekday: int) -> date:
    today = timezone.localdate()
    return today + timedelta(days=(weekday - today.weekday()) % 7 or 7)


@pytest.mark.django_db
def test_closed_on_sunday(clinic):
    service, vet = Service.objects.get(name="Wellness exam"), Vet.objects.first()
    assert available_slots(service, vet, next_weekday(6)) == []


@pytest.mark.django_db
def test_saturday_closes_at_13(clinic):
    service, vet = Service.objects.get(name="Wellness exam"), Vet.objects.first()
    slots = available_slots(service, vet, next_weekday(5))
    assert slots[0].hour == 9 and slots[-1].strftime("%H:%M") == "12:30"


@pytest.mark.django_db
def test_booked_time_is_not_offered(clinic, tutor):
    exam = Service.objects.get(name="Wellness exam")
    dental = Service.objects.get(name="Dental cleaning")  # 90 minutes
    vet = Vet.objects.first()
    day = next_weekday(1)
    starts = datetime.combine(day, datetime.min.time(), tzinfo=timezone.get_current_timezone()).replace(hour=10)
    Appointment.objects.create(pet=tutor.pets.first(), vet=vet, service=dental, starts_at=starts)

    times = {s.strftime("%H:%M") for s in available_slots(exam, vet, day)}
    assert {"10:00", "10:30", "11:00"}.isdisjoint(times)
    assert {"09:30", "11:30"} <= times


@pytest.mark.django_db
def test_cancelled_appointments_free_the_slot(clinic, tutor):
    exam, vet, day = Service.objects.get(name="Wellness exam"), Vet.objects.first(), next_weekday(2)
    starts = datetime.combine(day, datetime.min.time(), tzinfo=timezone.get_current_timezone()).replace(hour=9)
    Appointment.objects.create(
        pet=tutor.pets.first(), vet=vet, service=exam, starts_at=starts, status=Appointment.Status.CANCELLED
    )
    assert starts in available_slots(exam, vet, day)
