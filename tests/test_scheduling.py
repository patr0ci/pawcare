from datetime import date, datetime, timedelta

import pytest
from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.utils import timezone

from clinic.models import Appointment, Pet, Service, Tutor, Vet, available_slots


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


@pytest.mark.django_db
def test_slot_search_covers_the_whole_day(clinic, tutor):
    from clinic.services import find_slots

    biscuit = tutor.pets.get(name="Biscuit")
    exam = Service.objects.get(name="Wellness exam")
    slots = find_slots(biscuit, exam, next_weekday(3))
    assert any(start.hour >= 15 for _, start in slots)  # afternoons were cut off by an "8 earliest" limit
    assert {vet.name for vet, _ in slots} == {"Dr. Maya Chen"}  # the surgeon does not do wellness exams


@pytest.mark.django_db
def test_same_day_afternoon_booking_proposal_is_accepted(clinic, tutor):
    from assistant.models import Conversation
    from assistant.tools import ToolRunner

    biscuit = tutor.pets.get(name="Biscuit")
    exam = Service.objects.get(name="Wellness exam")
    day = next_weekday(3)
    runner = ToolRunner(Conversation.objects.create(user=tutor.user))
    found = runner.tool_find_available_slots(biscuit.id, exam.id, day.isoformat())
    chen = next(v for v in found["vets"] if v["vet"] == "Dr. Maya Chen")
    late = chen["starts_at"][-1]
    assert late.endswith("17:30") or "T17:30" in late
    result = runner.tool_propose_booking(biscuit.id, exam.id, chen["vet_id"], late)
    assert result["status"] == "awaiting_client_confirmation"



@pytest.mark.django_db
def test_cleanup_frees_slots_booked_by_demo_accounts(tutor):
    exam, chen = Service.objects.get(name="Wellness exam"), Vet.objects.get(name="Dr. Maya Chen")
    biscuit = tutor.pets.get(name="Biscuit")
    day = timezone.make_aware(datetime.combine(next_weekday(1), datetime.min.time()))
    old = Appointment.objects.create(pet=biscuit, vet=chen, service=exam, starts_at=day.replace(hour=9))
    fresh = Appointment.objects.create(pet=biscuit, vet=chen, service=exam, starts_at=day.replace(hour=10))
    Appointment.objects.filter(id=old.id).update(created_at=timezone.now() - timedelta(hours=3))
    client = Tutor.objects.create(user=get_user_model().objects.create_user("real-client"))
    rex = Pet.objects.create(tutor=client, name="Rex", species=Pet.Species.DOG)
    real = Appointment.objects.create(pet=rex, vet=chen, service=exam, starts_at=day.replace(hour=11))
    Appointment.objects.filter(id=real.id).update(created_at=timezone.now() - timedelta(days=30))

    call_command("cleanup_demo_users", appointment_hours=2)
    assert set(Appointment.objects.values_list("id", flat=True)) == {fresh.id, real.id}
