import json
from datetime import timedelta
from types import SimpleNamespace

import pytest
from django.urls import reverse
from django.utils import timezone

from assistant import chat
from assistant.chat import answer
from assistant.llm import Delta, Done, OpenAICompatibleLLM, ToolCall, Usage
from assistant.models import Conversation, PendingAction
from clinic.demo import create_demo_tutor
from clinic.models import Appointment, Service, Vet
from tests.test_scheduling import next_weekday


class ScriptedLLM:
    """Plays back one round per call: a list of tool calls, or a final text."""

    model = "scripted"

    def __init__(self, rounds):
        self.rounds = list(rounds)
        self.seen_messages = []

    def stream(self, messages, tools=None):
        self.seen_messages.append(list(messages))
        step = self.rounds.pop(0)
        if isinstance(step, str):
            yield Delta(step)
            yield Done(Usage(100, 20), self.model)
        else:
            calls = [ToolCall(id=f"call_{i}", name=n, arguments=json.dumps(a)) for i, (n, a) in enumerate(step)]
            yield Done(Usage(100, 10), self.model, tool_calls=calls)


@pytest.fixture
def script(monkeypatch):
    def install(*rounds):
        llm = ScriptedLLM(rounds)
        monkeypatch.setattr(chat, "get_llm", lambda: llm)
        return llm

    return install


def tool_results(llm):
    return [json.loads(m["content"]) for m in llm.seen_messages[-1] if m["role"] == "tool"]


def morning_slot(pet, service, day):
    from clinic.services import find_slots

    vet, start = find_slots(pet, service, day)[0]
    return vet, start


@pytest.mark.django_db
def test_booking_is_only_proposed_until_confirmed(client, tutor, script):
    biscuit = tutor.pets.get(name="Biscuit")
    exam = Service.objects.get(name="Wellness exam")
    day = next_weekday(1)
    vet, start = morning_slot(biscuit, exam, day)
    llm = script(
        [("list_my_pets", {}), ("list_services", {})],
        [("find_available_slots", {"pet_id": biscuit.id, "service_id": exam.id, "date": day.isoformat()})],
        [("propose_booking", {"pet_id": biscuit.id, "service_id": exam.id, "vet_id": vet.id,
                              "starts_at": timezone.localtime(start).isoformat()})],
        "I found a slot. Please confirm.",
    )
    conversation = Conversation.objects.create(user=tutor.user)
    events = list(answer(conversation, "Book a wellness exam for Biscuit"))

    actions = [e for e in events if e["type"] == "action"]
    assert len(actions) == 1 and "Biscuit" in actions[0]["summary"]
    assert [e["label"] for e in events if e["type"] == "tool"][:2] == ["Looking up your pets", "Checking services"]
    assert not Appointment.objects.exists()  # nothing written yet
    reply = conversation.messages.last()
    assert [c["name"] for c in reply.tool_calls] == ["list_my_pets", "list_services", "find_available_slots", "propose_booking"]
    assert reply.prompt_tokens == 400

    client.force_login(tutor.user)
    response = client.post(reverse("assistant:confirm_action", args=[actions[0]["id"]]))
    assert response.json()["status"] == "confirmed"
    appointment = Appointment.objects.get()
    assert (appointment.pet, appointment.vet, appointment.starts_at) == (biscuit, vet, start)

    # A confirmed action can't be replayed.
    assert client.post(reverse("assistant:confirm_action", args=[actions[0]["id"]])).status_code == 404


@pytest.mark.django_db
def test_tools_cannot_touch_another_clients_pets(tutor, script):
    stranger_pet = create_demo_tutor().pets.first()
    exam = Service.objects.get(name="Wellness exam")
    llm = script(
        [("find_available_slots", {"pet_id": stranger_pet.id, "service_id": exam.id, "date": next_weekday(1).isoformat()})],
        "Sorry.",
    )
    list(answer(Conversation.objects.create(user=tutor.user), "slots for pet"))
    assert tool_results(llm) == [{"error": "That pet isn't on your account. Call list_my_pets for the right id."}]


@pytest.mark.django_db
def test_other_users_cannot_confirm_my_action(client, tutor):
    conversation = Conversation.objects.create(user=tutor.user)
    action = PendingAction.objects.create(conversation=conversation, kind="cancel", payload={}, summary="x")
    client.force_login(create_demo_tutor().user)
    assert client.post(reverse("assistant:confirm_action", args=[action.id])).status_code == 404


@pytest.mark.django_db
def test_unavailable_time_is_rejected_at_proposal(tutor, script):
    biscuit = tutor.pets.get(name="Biscuit")
    exam = Service.objects.get(name="Wellness exam")
    sunday = timezone.localtime().replace(hour=10, minute=0, second=0, microsecond=0)
    sunday += timedelta(days=(6 - sunday.weekday()) % 7 or 7)
    llm = script(
        [("propose_booking", {"pet_id": biscuit.id, "service_id": exam.id, "vet_id": Vet.objects.first().id,
                              "starts_at": sunday.isoformat()})],
        "That didn't work.",
    )
    list(answer(Conversation.objects.create(user=tutor.user), "book sunday"))
    assert "isn't available" in tool_results(llm)[0]["error"]
    assert not PendingAction.objects.exists()


@pytest.mark.django_db
def test_slot_taken_between_proposal_and_confirmation(client, tutor, script):
    biscuit, miso = tutor.pets.get(name="Biscuit"), tutor.pets.get(name="Miso")
    exam = Service.objects.get(name="Wellness exam")
    vet, start = morning_slot(biscuit, exam, next_weekday(2))
    script(
        [("propose_booking", {"pet_id": biscuit.id, "service_id": exam.id, "vet_id": vet.id,
                              "starts_at": start.isoformat()})],
        "Please confirm.",
    )
    events = list(answer(Conversation.objects.create(user=tutor.user), "book"))
    Appointment.objects.create(pet=miso, vet=vet, service=exam, starts_at=start)  # someone else got there first

    client.force_login(tutor.user)
    action_id = next(e["id"] for e in events if e["type"] == "action")
    data = client.post(reverse("assistant:confirm_action", args=[action_id])).json()
    assert data["status"] == "failed" and "no longer available" in data["message"]
    assert Appointment.objects.count() == 1


@pytest.mark.django_db
def test_late_cancellation_mentions_fee_and_dismiss_changes_nothing(client, tutor, script):
    biscuit = tutor.pets.get(name="Biscuit")
    exam = Service.objects.get(name="Wellness exam")
    soon = timezone.now() + timedelta(hours=3)
    appointment = Appointment.objects.create(pet=biscuit, vet=Vet.objects.first(), service=exam, starts_at=soon)
    script([("propose_cancellation", {"appointment_id": appointment.id})], "Please confirm.")
    events = list(answer(Conversation.objects.create(user=tutor.user), "cancel it"))
    action = next(e for e in events if e["type"] == "action")
    assert "$25 fee" in action["summary"]

    client.force_login(tutor.user)
    assert client.post(reverse("assistant:dismiss_action", args=[action["id"]])).json()["status"] == "dismissed"
    appointment.refresh_from_db()
    assert appointment.status == Appointment.Status.SCHEDULED


@pytest.mark.django_db
def test_reschedule_into_overlapping_time_of_same_appointment(client, tutor, script):
    biscuit = tutor.pets.get(name="Biscuit")
    dental = Service.objects.get(name="Dental cleaning")  # 90 min
    vet = Vet.objects.get(name="Dr. Maya Chen")
    day = next_weekday(3)
    start = timezone.make_aware(timezone.datetime.combine(day, timezone.datetime.min.time())).replace(hour=10)
    appointment = Appointment.objects.create(pet=biscuit, vet=vet, service=dental, starts_at=start)
    script([("propose_reschedule", {"appointment_id": appointment.id, "starts_at": (start + timedelta(minutes=30)).isoformat()})], "ok")
    events = list(answer(Conversation.objects.create(user=tutor.user), "move it 30 min later"))

    client.force_login(tutor.user)
    action_id = next(e["id"] for e in events if e["type"] == "action")
    assert client.post(reverse("assistant:confirm_action", args=[action_id])).json()["status"] == "confirmed"
    appointment.refresh_from_db()
    assert appointment.starts_at == start + timedelta(minutes=30)
    assert appointment.status == Appointment.Status.SCHEDULED


@pytest.mark.django_db
def test_expired_proposal_is_not_executed(client, tutor):
    conversation = Conversation.objects.create(user=tutor.user)
    action = PendingAction.objects.create(conversation=conversation, kind="cancel", payload={"appointment_id": 1}, summary="x")
    PendingAction.objects.filter(id=action.id).update(created_at=timezone.now() - timedelta(hours=1))
    client.force_login(tutor.user)
    assert client.post(reverse("assistant:confirm_action", args=[action.id])).json()["status"] == "failed"


@pytest.mark.django_db
def test_runaway_tool_loop_is_cut_off(tutor, script):
    script(*([[("list_my_pets", {})]] * chat.MAX_TOOL_ROUNDS))
    events = list(answer(Conversation.objects.create(user=tutor.user), "loop"))
    assert "too many steps" in "".join(e["text"] for e in events if e["type"] == "delta")
    assert events[-1]["type"] == "done"


def test_streamed_tool_call_fragments_are_reassembled():
    def chunk(content=None, tool_calls=None, usage=None):
        delta = SimpleNamespace(content=content, tool_calls=tool_calls)
        return SimpleNamespace(choices=[SimpleNamespace(delta=delta)] if usage is None else [], usage=usage, model="m")

    def frag(index, id=None, name=None, args=None):
        return SimpleNamespace(index=index, id=id, function=SimpleNamespace(name=name, arguments=args))

    stream = [
        chunk(tool_calls=[frag(0, "a", "find_available_slots", '{"pet_id": 1,')]),
        chunk(tool_calls=[frag(0, args=' "date": "2026-10-13"}'), frag(1, "b", "list_services", "{}")]),
        chunk(usage=SimpleNamespace(prompt_tokens=50, completion_tokens=5)),
    ]
    llm = OpenAICompatibleLLM.__new__(OpenAICompatibleLLM)
    llm.model = "m"
    llm.client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=lambda **kw: iter(stream))))
    done = list(llm.stream([], tools=[]))[-1]
    assert [(c.id, c.name, json.loads(c.arguments)) for c in done.tool_calls] == [
        ("a", "find_available_slots", {"pet_id": 1, "date": "2026-10-13"}),
        ("b", "list_services", {}),
    ]
    assert done.usage.prompt_tokens == 50


@pytest.mark.django_db
def test_text_from_separate_rounds_is_not_glued_together(tutor, monkeypatch):
    class Narrating(ScriptedLLM):
        def stream(self, messages, tools=None):
            step = self.rounds.pop(0)
            if step == "narrate":
                yield Delta("Let me check your pets first.")
                yield Done(Usage(1, 1), "m", tool_calls=[ToolCall("c", "list_my_pets", "{}")])
            else:
                yield Delta(step)
                yield Done(Usage(1, 1), "m")

    llm = Narrating(["narrate", "Biscuit is a Beagle."])
    monkeypatch.setattr(chat, "get_llm", lambda: llm)
    events = list(answer(Conversation.objects.create(user=tutor.user), "pets?"))
    text = "".join(e["text"] for e in events if e["type"] == "delta")
    assert text == "Let me check your pets first.\n\nBiscuit is a Beagle."


@pytest.mark.django_db
def test_empty_model_reply_gets_a_fallback(tutor, script):
    script("")
    events = list(answer(Conversation.objects.create(user=tutor.user), "hm"))
    assert "rephrase" in "".join(e["text"] for e in events if e["type"] == "delta")
