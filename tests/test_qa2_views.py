"""Regression tests for the second QA round: chat, dashboard, admin and the 403 page."""

from datetime import timedelta

import pytest
from django.urls import reverse
from django.utils import timezone

from assistant import chat
from assistant.models import Conversation, Message, PendingAction
from clinic.demo import create_demo_tutor
from tests.test_views import read_events


def proposal(user, **fields):
    conversation = Conversation.objects.create(user=user)
    return PendingAction.objects.create(conversation=conversation, kind="cancel", payload={}, summary="x", **fields)


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
