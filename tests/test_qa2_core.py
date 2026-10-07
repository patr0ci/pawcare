"""Regression tests for the second QA round (assistant core: guardrail, tools, interrupted answers, evals)."""

import json
from datetime import datetime, time, timedelta

import pytest
from django.urls import reverse
from django.utils import timezone

from assistant import chat
from assistant.chat import answer
from assistant.llm import Delta, Done, ToolCall, Usage
from assistant.models import Conversation, Message, PendingAction
from assistant.tools import fmt
from clinic.models import Appointment, Service, Vet
from tests.test_agent import ScriptedLLM, morning_slot
from tests.test_scheduling import next_weekday
from tests.test_views import read_events


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


class StreamingLLM(ScriptedLLM):
    """Each round is a list of events; an exception in the list is raised at that point (a dropped stream)."""

    def stream(self, messages, tools=None):
        self.seen_messages.append(list(messages))
        for event in self.rounds.pop(0):
            if isinstance(event, Exception):
                raise event
            yield event


@pytest.mark.django_db
def test_closing_the_tab_mid_answer_saves_a_marked_reply_with_an_estimated_cost(tutor, monkeypatch, settings):
    settings.LLM_PRICE_INPUT_PER_M, settings.LLM_PRICE_OUTPUT_PER_M = 1.0, 2.0
    llm = StreamingLLM([[Delta("Rabies costs $28."), Delta(" DHPP costs $35."), Done(Usage(100, 20), "m")]])
    monkeypatch.setattr(chat, "get_llm", lambda: llm)
    conversation = Conversation.objects.create(user=tutor.user)
    stream = answer(conversation, "vaccine prices?")
    assert [next(stream)["type"], next(stream)["type"]] == ["sources", "delta"]
    stream.close()  # what the server does when the visitor closes the tab

    reply = conversation.messages.get(role="assistant")
    assert reply.content == "Rabies costs $28.\n\n(answer interrupted)"
    # Usage only comes with Done, so the round is estimated instead of counting as free.
    assert reply.prompt_tokens == len(json.dumps(llm.seen_messages[0])) // 4
    assert reply.completion_tokens == len("Rabies costs $28.") // 4
    assert reply.cost_usd > 0


@pytest.mark.django_db
def test_a_dropped_stream_never_saves_text_that_was_held_back(tutor, monkeypatch):
    llm = StreamingLLM(
        [
            [Delta("Let me check."), Done(Usage(100, 10), "m", tool_calls=[ToolCall("c", "list_my_pets", "{}")])],
            [Delta("I've booked Biscuit for 9:00."), RuntimeError("stream dropped")],  # not checked yet
        ]
    )
    monkeypatch.setattr(chat, "get_llm", lambda: llm)
    conversation = Conversation.objects.create(user=tutor.user)
    shown = []
    with pytest.raises(RuntimeError):
        for event in answer(conversation, "book Biscuit"):
            shown += [event["text"]] if event["type"] == "delta" else []

    reply = conversation.messages.get(role="assistant")
    assert shown == ["Let me check."]
    assert reply.content == "Let me check.\n\n(answer interrupted)"
    assert reply.completion_tokens == 10 + len("\n\nI've booked Biscuit for 9:00.") // 4


@pytest.mark.django_db
def test_a_reply_that_failed_before_any_text_reads_like_the_live_error(client, tutor, monkeypatch):
    monkeypatch.setattr(chat, "get_llm", lambda: StreamingLLM([[RuntimeError("provider down")]]))
    client.force_login(tutor.user)
    events = read_events(client.post(reverse("assistant:send_message"), {"message": "hi"}))
    reply = Message.objects.get(role="assistant")
    assert events[-1] == {"type": "error", "message": reply.content}
    assert reply.cost_usd == 0  # nothing came back: most likely rejected, so not billed


def test_dates_outside_this_year_show_the_year():
    now = timezone.localtime()
    next_year = now.replace(year=now.year + 1, month=1, day=5, hour=9, minute=0)
    assert fmt(next_year) == f"{next_year:%a} Jan 5, {now.year + 1}, 9:00 AM"
    this_year = now.replace(hour=14, minute=30)
    assert fmt(this_year) == f"{this_year:%a %b %-d}, 2:30 PM"


@pytest.mark.django_db
def test_reschedule_card_shows_the_old_time_and_the_new_one(tutor, script):
    tuesday = next_weekday(1) + timedelta(weeks=1)  # far enough out for no late-change fee
    old = timezone.make_aware(datetime.combine(tuesday, time(9)))
    new = old + timedelta(days=3, hours=5)  # Friday, 2:00 PM
    appointment = Appointment.objects.create(
        pet=tutor.pets.get(name="Biscuit"),
        vet=Vet.objects.get(name="Dr. Maya Chen"),
        service=Service.objects.get(name="Wellness exam"),
        starts_at=old,
    )
    script(
        [("propose_reschedule", {"appointment_id": appointment.id, "starts_at": new.isoformat()})], "Please confirm."
    )
    events = list(answer(Conversation.objects.create(user=tutor.user), "move it to Friday afternoon"))
    summary = next(e["summary"] for e in events if e["type"] == "action")
    assert summary == f"Move Biscuit's Wellness exam from {fmt(old)} to {fmt(new)}"
    assert fmt(old).endswith("9:00 AM") and fmt(new).endswith("2:00 PM")
