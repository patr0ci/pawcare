"""Regression tests for the second QA round: chat, dashboard, admin and the 403 page."""

from datetime import timedelta

import pytest
from django.contrib.auth import get_user_model
from django.urls import reverse
from django.utils import timezone

from assistant import chat
from assistant.models import Conversation, Message, PendingAction
from clinic.demo import create_demo_tutor
from tests.test_views import read_events


def proposal(user, summary="x", **fields):
    conversation = Conversation.objects.create(user=user)
    return PendingAction.objects.create(conversation=conversation, kind="cancel", payload={}, summary=summary, **fields)


@pytest.mark.django_db
def test_chat_page_is_never_cached(client, tutor):
    # Back from My pets used to show the cached page, with a card still asking to confirm a booking made since.
    client.force_login(tutor.user)
    assert "no-store" in client.get(reverse("assistant:chat"))["Cache-Control"]


@pytest.mark.django_db
def test_a_settled_proposal_says_what_happened_to_it(client, tutor):
    confirmed = proposal(tutor.user, status="confirmed", result="Booked.")
    dismissed = proposal(tutor.user, status="dismissed", result="Okay, I didn't change anything.")
    expired = proposal(tutor.user, status="failed", result="That proposal expired. Please ask again.")
    client.force_login(tutor.user)
    expected = {
        confirmed: {"status": "confirmed", "message": "This proposal was already confirmed."},
        dismissed: {"status": "dismissed", "message": "This proposal was already dismissed."},
        expired: {"status": "failed", "message": "That proposal expired. Please ask again."},
    }
    for action, body in expected.items():
        for verb in ("confirm_action", "dismiss_action"):
            response = client.post(reverse(f"assistant:{verb}", args=[action.id]))
            assert (response.status_code, response.json()) == (409, body)
            action.refresh_from_db()
            assert action.status == body["status"]
    assert not Message.objects.exists()  # nothing new happened, so nothing is logged in the conversation


@pytest.mark.django_db
def test_a_second_click_gets_the_same_status_and_is_logged_once(client, tutor):
    action = proposal(tutor.user)
    client.force_login(tutor.user)
    first = client.post(reverse("assistant:dismiss_action", args=[action.id]))
    second = client.post(reverse("assistant:dismiss_action", args=[action.id]))
    assert first.status_code == 200 and second.status_code == 409
    assert first.json()["status"] == second.json()["status"] == "dismissed"
    assert Message.objects.count() == 1  # the outcome, logged once


@pytest.mark.django_db
def test_other_users_still_get_404_for_settled_proposals(client, tutor):
    action = proposal(tutor.user, status="confirmed", result="Booked.")
    client.force_login(create_demo_tutor().user)
    for verb in ("confirm_action", "dismiss_action"):
        assert client.post(reverse(f"assistant:{verb}", args=[action.id])).status_code == 404


@pytest.mark.django_db
def test_expired_pending_proposal_is_still_marked_failed_on_confirm(client, tutor):
    action = proposal(tutor.user)
    PendingAction.objects.filter(id=action.id).update(created_at=timezone.now() - timedelta(hours=1))
    client.force_login(tutor.user)
    response = client.post(reverse("assistant:confirm_action", args=[action.id]))
    assert response.status_code == 200 and response.json()["status"] == "failed"
    again = client.post(reverse("assistant:confirm_action", args=[action.id]))
    assert again.status_code == 409 and "expired" in again.json()["message"]


@pytest.mark.django_db
def test_error_event_reports_remaining_messages(client, tutor, articles, settings, monkeypatch):
    class ProviderDown:
        model = "down"

        def stream(self, messages, tools=None):
            raise RuntimeError("provider down")
            yield

    settings.ASSISTANT_DAILY_MESSAGE_LIMIT = 5
    monkeypatch.setattr(chat, "get_llm", lambda: ProviderDown())
    client.force_login(tutor.user)
    events = read_events(client.post(reverse("assistant:send_message"), {"message": "rabies vaccine price"}))
    # The question was saved before the provider failed, so it counts: the "messages left" counter must say so.
    assert events[-1]["type"] == "error" and events[-1]["remaining"] == 4


@pytest.mark.django_db
def test_nul_characters_are_dropped_before_validation(client, tutor, articles):
    # Postgres can't store NUL: it used to pass validation and fail only after retrieval, as "unavailable".
    client.force_login(tutor.user)
    response = client.post(reverse("assistant:send_message"), {"message": "\x00 \x00"})
    assert response.status_code == 400 and "1000" in response.json()["error"]
    events = read_events(client.post(reverse("assistant:send_message"), {"message": "rabies\x00 vaccine price"}))
    assert events[-1]["type"] == "done"
    assert Message.objects.get(role="user").content == "rabies vaccine price"


@pytest.mark.django_db
def test_reload_shows_a_turns_cards_after_its_reply_in_proposal_order(client, tutor):
    client.force_login(tutor.user)
    conversation = Conversation.objects.create(user=tutor.user)
    session = client.session
    session["conversation_id"] = conversation.id
    session.save()
    Message.objects.create(conversation=conversation, role="user", content="move both visits")
    for summary in ("Move Biscuit", "Move Miso"):
        PendingAction.objects.create(conversation=conversation, kind="reschedule", payload={}, summary=summary)
    Message.objects.create(conversation=conversation, role="assistant", content="Please confirm both.")
    html = client.get(reverse("assistant:chat")).content.decode()
    assert html.index("Please confirm both.") < html.index("Move Biscuit") < html.index("Move Miso")


def staff_client(client):
    client.force_login(get_user_model().objects.create_superuser("boss", password="x"))
    return client


@pytest.mark.django_db
def test_dashboard_counts_tool_calls_in_the_database(client, tutor):
    conversation = Conversation.objects.create(user=tutor.user)
    traces = [
        [{"name": "list_my_pets"}, {"name": "find_available_slots"}, {"name": "list_my_pets"}],
        [{"name": "guardrail"}, {"name": "list_my_pets"}],
        [],
    ]
    for trace in traces:
        Message.objects.create(conversation=conversation, role="assistant", content="a", model="m", tool_calls=trace)
    old = Message.objects.create(
        conversation=conversation, role="assistant", content="a", model="m", tool_calls=[{"name": "list_services"}]
    )
    Message.objects.filter(id=old.id).update(created_at=timezone.now() - timedelta(days=31))
    staff_client(client)
    response = client.get(reverse("assistant:dashboard"))
    # The guardrail is a correction round, not a tool the model chose; it's shown on its own.
    assert response.context["tools"] == [("list_my_pets", 3), ("find_available_slots", 1)]
    assert response.context["guardrail"] == 1
    html = response.content.decode()
    assert "<code>guardrail</code>" not in html and "Guardrail corrections: 1" in html


@pytest.mark.django_db
def test_dashboard_top_users_leave_out_deleted_accounts(client, tutor):
    for user in (tutor.user, None, None):
        conversation = Conversation.objects.create(user=user)
        Message.objects.create(conversation=conversation, role="assistant", content="a", model="m", cost_usd="0.01")
    staff_client(client)
    response = client.get(reverse("assistant:dashboard"))
    assert [u["conversation__user__username"] for u in response.context["top_users"]] == [tutor.user.username]
    assert response.context["totals"]["answers"] == 3  # still counted in the totals


@pytest.mark.django_db
def test_dashboard_shows_old_pending_proposals_as_expired(client, tutor):
    stale = proposal(tutor.user)
    PendingAction.objects.filter(id=stale.id).update(
        created_at=timezone.now() - PendingAction.TTL - timedelta(minutes=1)
    )
    proposal(tutor.user)
    proposal(tutor.user, status="confirmed")
    PendingAction.objects.create(conversation=stale.conversation, kind="book", payload={}, summary="y")
    staff_client(client)
    rows = [(a["kind"], a["state"], a["n"]) for a in client.get(reverse("assistant:dashboard")).context["actions"]]
    assert rows == [
        ("book", "pending", 1),
        ("cancel", "confirmed", 1),
        ("cancel", "expired", 1),
        ("cancel", "pending", 1),
    ]


@pytest.mark.django_db
def test_dashboard_empty_tables_and_big_numbers(client, tutor):
    staff_client(client)
    html = client.get(reverse("assistant:dashboard")).content.decode()
    assert html.count("None yet") == 4  # by model, top users, tool calls, proposed actions
    conversation = Conversation.objects.create(user=tutor.user)
    Message.objects.create(
        conversation=conversation,
        role="assistant",
        content="a",
        model="m",
        prompt_tokens=1234567,
        completion_tokens=8901,
    )
    assert "1,234,567 / 8,901" in client.get(reverse("assistant:dashboard")).content.decode()


@pytest.mark.django_db
def test_budget_notice_names_the_clinic_time_zone(client, tutor, settings):
    settings.ASSISTANT_DAILY_BUDGET_USD = 0
    settings.TIME_ZONE = "America/Sao_Paulo"  # CLINIC_TIME_ZONE: the day (and the budget) resets at its midnight
    client.force_login(tutor.user)
    html = client.get(reverse("assistant:chat")).content.decode()
    assert "midnight, America/Sao Paulo time" in html and "US Eastern" not in html


@pytest.mark.django_db
def test_admin_conversation_totals_survive_a_search(client, tutor):
    # The search joined messages a second time, so the Count/Sum columns came out multiplied (3 messages read 9).
    conversation = Conversation.objects.create(user=tutor.user)
    for i in range(3):
        Message.objects.create(conversation=conversation, role="user", content=f"rabies {i}", cost_usd="0.001")
    empty = Conversation.objects.create(user=tutor.user)
    staff_client(client)
    url = reverse("admin:assistant_conversation_changelist")
    detail = (conversation.messages.count(), sum(m.cost_usd for m in conversation.messages.all()))
    for query in ("", "rabies", tutor.user.username):
        rows = {c.id: (c.n, c.cost_sum) for c in client.get(url, {"q": query}).context["cl"].result_list}
        assert rows[conversation.id] == detail, query
    ordered = client.get(url, {"o": "3"}).context["cl"].result_list  # by the messages column
    assert [(c.id, c.n, c.cost_sum) for c in ordered] == [(empty.id, 0, 0), (conversation.id, *detail)]


@pytest.mark.django_db
def test_admin_message_list_shows_whose_message_newest_first(client, tutor):
    conversation = Conversation.objects.create(user=tutor.user)
    Message.objects.create(conversation=conversation, role="user", content="first")
    Message.objects.create(conversation=conversation, role="assistant", content="word " * 100)
    staff_client(client)
    response = client.get(reverse("admin:assistant_message_changelist"))
    cl = response.context["cl"]
    assert {"conversation", "user"} <= set(cl.list_display)
    assert [m.content for m in cl.result_list][-1] == "first"
    html = response.content.decode()
    assert tutor.user.username in html and f"Conversation {conversation.id}" in html
    assert "word word…" in html


@pytest.mark.django_db
def test_admin_proposals_show_their_conversation_and_can_be_searched(client, tutor):
    biscuit = proposal(tutor.user, summary="Book Biscuit")
    proposal(tutor.user, summary="Cancel Miso")
    staff_client(client)
    response = client.get(reverse("admin:assistant_pendingaction_changelist"), {"q": "biscuit"})
    cl = response.context["cl"]
    assert [a.id for a in cl.result_list] == [biscuit.id]
    assert "conversation" in cl.list_display and cl.date_hierarchy == "created_at"
    assert f"Conversation {biscuit.conversation_id}" in response.content.decode()
