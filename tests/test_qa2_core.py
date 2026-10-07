"""Regression tests for the second QA round (assistant core: guardrail, tools, interrupted answers, evals)."""

from datetime import timedelta

import pytest
from django.urls import reverse
from django.utils import timezone

from assistant import chat
from assistant.chat import answer
from assistant.models import Conversation, PendingAction
from clinic.models import Appointment, Service, Vet
from tests.test_agent import ScriptedLLM, morning_slot
from tests.test_scheduling import next_weekday


@pytest.fixture
def script(monkeypatch):
    def install(*rounds):
        llm = ScriptedLLM(rounds)
        monkeypatch.setattr(chat, "get_llm", lambda: llm)
        return llm

    return install


def propose_booking(pet, service, day):
    vet, start = morning_slot(pet, service, day)
    args = {
        "pet_id": pet.id,
        "service_id": service.id,
        "vet_id": vet.id,
        "starts_at": timezone.localtime(start).isoformat(),
    }
    return [("propose_booking", args)]


@pytest.fixture
def biscuit_card_waiting(tutor, script):
    """A conversation whose previous turn left a Confirm card for Biscuit."""
    conversation = Conversation.objects.create(user=tutor.user)
    exam = Service.objects.get(name="Wellness exam")
    script(propose_booking(tutor.pets.get(name="Biscuit"), exam, next_weekday(1)), "Please click Confirm.")
    list(answer(conversation, "Book a wellness exam for Biscuit next Tuesday morning"))
    assert conversation.actions.get().status == PendingAction.Status.PENDING
    return conversation


@pytest.mark.django_db
def test_a_slot_search_means_a_new_action_so_the_old_card_excuses_no_claim(tutor, script, biscuit_card_waiting):
    # Seen in QA: with Biscuit's card waiting, "book Miso..." got "I've set up a proposal for 12:00. Please click
    # Confirm" with no new proposal, and the only Confirm on screen booked Biscuit.
    miso, exam, day = tutor.pets.get(name="Miso"), Service.objects.get(name="Wellness exam"), next_weekday(1)
    llm = script(
        [("find_available_slots", {"pet_id": miso.id, "service_id": exam.id, "date": day.isoformat()})],
        "I've set up a proposal for 12:00. Please click Confirm.",
        propose_booking(miso, exam, day),
        "Here it is: please click Confirm.",
    )
    events = list(answer(biscuit_card_waiting, "Now book Miso a wellness exam the same day"))

    assert llm.seen_messages[2][-1] == {"role": "user", "content": chat.UNBACKED_CLAIM_NUDGE}
    assert "".join(e["text"] for e in events if e["type"] == "delta") == "Here it is: please click Confirm."
    assert len([e for e in events if e["type"] == "action"]) == 1
    assert biscuit_card_waiting.actions.latest("created_at").payload["pet_id"] == miso.id


@pytest.mark.django_db
def test_pointing_to_the_waiting_card_needs_no_correction(script, biscuit_card_waiting):
    llm = script("Not yet: please click Confirm on the card above.")
    list(answer(biscuit_card_waiting, "did you book it?"))
    assert len(llm.seen_messages) == 1
    assert [c["name"] for c in biscuit_card_waiting.messages.last().tool_calls] == []


@pytest.mark.parametrize("reason", [None, 7, ["moving away"]])
@pytest.mark.django_db
def test_cancellation_with_an_odd_reason_can_still_be_confirmed(client, tutor, script, reason):
    # A null reason used to reach services.cancel as None, so None[:255] made every Confirm fail.
    appointment = Appointment.objects.create(
        pet=tutor.pets.get(name="Miso"),
        vet=Vet.objects.get(name="Dr. Maya Chen"),
        service=Service.objects.get(name="Vaccination visit"),
        starts_at=timezone.now() + timedelta(days=5),
    )
    script([("propose_cancellation", {"appointment_id": appointment.id, "reason": reason})], "Please confirm.")
    events = list(answer(Conversation.objects.create(user=tutor.user), "cancel Miso's visit"))
    action_id = next(e["id"] for e in events if e["type"] == "action")

    client.force_login(tutor.user)
    assert client.post(reverse("assistant:confirm_action", args=[action_id])).json()["status"] == "confirmed"
    appointment.refresh_from_db()
    assert appointment.status == Appointment.Status.CANCELLED
    assert appointment.cancellation_reason == ("" if reason is None else str(reason))
