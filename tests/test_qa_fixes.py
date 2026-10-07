"""Regression tests for the QA round of 2026-10-06 (one test per finding)."""

import json
from datetime import timedelta

import pytest
from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.urls import reverse
from django.utils import timezone

from assistant import chat
from assistant.chat import answer, build_messages
from assistant.evals.runner import run_evals
from assistant.models import Conversation, Message, PendingAction
from clinic.models import Appointment, Service, Vet
from clinic.services import BookingError, book, cancel, check_slot, late_change_fee, reschedule
from tests.test_agent import ScriptedLLM
from tests.test_scheduling import next_weekday


def at(day, hour, minute=0):
    return timezone.make_aware(timezone.datetime.combine(day, timezone.datetime.min.time())).replace(hour=hour, minute=minute)


@pytest.mark.django_db
def test_a_pet_cannot_be_in_two_places_at_once(tutor):
    biscuit = tutor.pets.get(name="Biscuit")
    exam, dental = Service.objects.get(name="Wellness exam"), Service.objects.get(name="Dental cleaning")
    chen, souza = Vet.objects.get(name="Dr. Maya Chen"), Vet.objects.get(name="Dr. Rafael Souza")
    day = next_weekday(1)
    book(tutor, biscuit.id, dental.id, souza.id, at(day, 10))
    with pytest.raises(BookingError, match="already booked"):
        book(tutor, biscuit.id, exam.id, chen.id, at(day, 10, 30))


@pytest.mark.django_db
def test_vets_only_offer_their_own_services(tutor):
    biscuit = tutor.pets.get(name="Biscuit")
    with pytest.raises(BookingError, match="doesn't do wellness exam"):
        check_slot(biscuit, Vet.objects.get(name="Dr. Rafael Souza"), Service.objects.get(name="Wellness exam"), at(next_weekday(1), 10))


@pytest.mark.django_db
def test_past_and_completed_appointments_are_history(tutor):
    biscuit, exam, chen = tutor.pets.get(name="Biscuit"), Service.objects.get(name="Wellness exam"), Vet.objects.get(name="Dr. Maya Chen")
    past = Appointment.objects.create(pet=biscuit, vet=chen, service=exam, starts_at=timezone.now() - timedelta(days=3))
    done = Appointment.objects.create(pet=biscuit, vet=chen, service=exam, starts_at=at(next_weekday(2), 9),
                                      status=Appointment.Status.COMPLETED)
    for appointment in (past, done):
        with pytest.raises(BookingError):
            cancel(tutor, appointment.id)
        with pytest.raises(BookingError):
            reschedule(tutor, appointment.id, at(next_weekday(3), 11))
    assert late_change_fee(past) == 0


@pytest.mark.django_db
def test_reschedule_proposal_checks_the_slot_up_front(tutor, monkeypatch):
    biscuit, exam, chen = tutor.pets.get(name="Biscuit"), Service.objects.get(name="Wellness exam"), Vet.objects.get(name="Dr. Maya Chen")
    day = next_weekday(3)
    mine = Appointment.objects.create(pet=biscuit, vet=chen, service=exam, starts_at=at(day, 9))
    Appointment.objects.create(pet=tutor.pets.get(name="Miso"), vet=chen, service=exam, starts_at=at(day, 11))
    llm = ScriptedLLM([[("propose_reschedule", {"appointment_id": mine.id, "starts_at": at(day, 11).isoformat()})], "x"])
    monkeypatch.setattr(chat, "get_llm", lambda: llm)
    list(answer(Conversation.objects.create(user=tutor.user), "move it"))
    tool_result = json.loads([m for m in llm.seen_messages[-1] if m["role"] == "tool"][0]["content"])
    assert "no longer available" in tool_result["error"]
    assert not PendingAction.objects.exists()


@pytest.mark.django_db
def test_confirm_survives_unexpected_errors_and_long_text(client, tutor):
    conversation = Conversation.objects.create(user=tutor.user)
    action = PendingAction.objects.create(conversation=conversation, kind="book", summary="x" * 500,
                                          payload={"pet_id": 1, "service_id": 999999, "vet_id": 1, "starts_at": "2030-01-01T10:00"})
    client.force_login(tutor.user)
    data = client.post(reverse("assistant:confirm_action", args=[action.id])).json()
    assert data["status"] == "failed" and "ask the assistant again" in data["message"]
    action.refresh_from_db()
    assert action.status == "failed"


@pytest.mark.django_db
def test_usage_is_saved_when_the_provider_fails_mid_answer(tutor, monkeypatch):
    class FailsSecondRound(ScriptedLLM):
        def stream(self, messages, tools=None):
            if self.rounds == ["boom"]:
                raise RuntimeError("provider down")
            yield from super().stream(messages, tools)

    llm = FailsSecondRound([[("list_my_pets", {})], "boom"])
    monkeypatch.setattr(chat, "get_llm", lambda: llm)
    conversation = Conversation.objects.create(user=tutor.user)
    with pytest.raises(RuntimeError):
        list(answer(conversation, "pets?"))
    reply = conversation.messages.get(role="assistant")
    assert reply.prompt_tokens == 100 and reply.tool_calls[0]["name"] == "list_my_pets"


@pytest.mark.django_db
def test_history_merges_consecutive_turns_of_the_same_role(tutor):
    conversation = Conversation.objects.create(user=tutor.user)
    for role, text in [("user", "book it"), ("assistant", "please confirm"), ("assistant", "Booked: ...")]:
        Message.objects.create(conversation=conversation, role=role, content=text)
    roles = [m["role"] for m in build_messages(conversation, "thanks", [])]
    assert roles == ["system", "user", "assistant", "user"]


@pytest.mark.django_db
def test_eval_run_survives_a_failing_case(articles, tmp_path, monkeypatch):
    def flaky(conversation, question):
        if "boom" in question:
            raise RuntimeError("provider down")
        return chat.answer(conversation, question)

    import assistant.evals.runner as runner

    monkeypatch.setattr(runner, "answer", flaky)
    cases = tmp_path / "cases.json"
    cases.write_text(json.dumps([{"question": "boom"}, {"question": "rabies vaccine price", "must_include": ["$28"]}]))
    run = run_evals(cases, agent_cases_path=None)
    assert (run.total, run.passed) == (2, 1)
    assert run.results[0]["failures"][0].startswith("error: RuntimeError")


@pytest.mark.django_db
def test_deleting_demo_accounts_keeps_cost_history(tutor):
    conversation = Conversation.objects.create(user=tutor.user)
    Message.objects.create(conversation=conversation, role="assistant", content="x", cost_usd="0.001")
    get_user_model().objects.filter(id=tutor.user.id).update(date_joined=timezone.now() - timedelta(days=5))
    call_command("cleanup_demo_users", days=2)
    assert not get_user_model().objects.filter(id=tutor.user.id).exists()
    assert Message.objects.get().cost_usd > 0


@pytest.mark.django_db
def test_new_conversation_link_redirects_so_reload_keeps_it(client, tutor):
    client.force_login(tutor.user)
    response = client.get(reverse("assistant:chat") + "?new=1")
    assert response.status_code == 302 and response.url == reverse("assistant:chat")


@pytest.mark.django_db
def test_reload_shows_resolved_action_cards_in_order(client, tutor):
    conversation = Conversation.objects.create(user=tutor.user)
    session = client.session
    client.force_login(tutor.user)
    session = client.session
    session["conversation_id"] = conversation.id
    session.save()
    Message.objects.create(conversation=conversation, role="user", content="book it")
    PendingAction.objects.create(conversation=conversation, kind="book", payload={}, summary="Book exam", status="confirmed")
    Message.objects.create(conversation=conversation, role="assistant", content="Please confirm.")
    Message.objects.create(conversation=conversation, role="assistant", content="Booked: exam")
    html = client.get(reverse("assistant:chat")).content.decode()
    assert html.index("book it") < html.index("Please confirm.") < html.index("Book exam") < html.index("Booked: exam")
    assert "0 tokens" not in html  # outcome messages have no model/usage line


@pytest.mark.django_db
def test_public_dashboard_hides_other_visitors_names(client, tutor, settings):
    settings.DEMO_PUBLIC_DASHBOARD = True
    conversation = Conversation.objects.create(user=tutor.user)
    Message.objects.create(conversation=conversation, role="assistant", content="x", model="m", cost_usd="0.001")
    viewer = get_user_model().objects.create_user("demo-viewer")
    client.force_login(viewer)
    html = client.get(reverse("assistant:dashboard")).content.decode()
    assert "visitor 1" in html and tutor.user.username not in html


@pytest.mark.django_db
def test_article_lists_and_cross_links(client, db):
    from helpcenter.models import Article

    Article.objects.create(slug="vaccine-prices", title="Vaccine Prices", category="V", body="Prices.")
    Article.objects.create(slug="services", title="Services", category="P",
                           body='Our services:\n- Exam: $65\n- Nail trim: $20\n\nSee "Vaccine Prices". <b>not bold</b>')
    html = client.get("/help/services/").content.decode()
    assert "<ul><li>Exam: $65</li>" in html
    assert '<a href="/help/vaccine-prices/">Vaccine Prices</a>' in html
    assert "&lt;b&gt;not bold&lt;/b&gt;" in html


def test_bad_help_file_is_skipped_not_fatal(db, tmp_path):
    from helpcenter.ingest import ingest_directory

    (tmp_path / "good.md").write_text("---\ntitle: Good\ncategory: X\n---\nBody")
    (tmp_path / "bad.md").write_text("no front matter")
    stats = ingest_directory(tmp_path)
    assert (stats["created"], stats["errors"]) == (1, 1)


@pytest.mark.django_db
def test_demo_login_honours_safe_next_only(client, clinic):
    assert client.post(reverse("demo_login"), {"next": "/help/"}).url == "/help/"
    client.logout()
    assert client.post(reverse("demo_login"), {"next": "https://evil.example/"}).url == reverse("assistant:chat")


@pytest.mark.django_db
def test_done_event_reports_remaining_messages(articles, tutor, settings):
    settings.ASSISTANT_DAILY_MESSAGE_LIMIT = 10
    events = list(answer(Conversation.objects.create(user=tutor.user), "rabies vaccine price"))
    assert events[-1]["remaining"] == 9
