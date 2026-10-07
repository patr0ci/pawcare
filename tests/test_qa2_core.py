"""Regression tests for the second QA round (assistant core: guardrail, tools, interrupted answers, evals)."""

import pytest
from django.utils import timezone

from assistant import chat
from assistant.chat import answer
from assistant.models import Conversation, PendingAction
from clinic.models import Service
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
