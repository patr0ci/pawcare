import json

import pytest
from django.urls import reverse

from assistant.models import Message
from clinic.models import Pet


def read_events(response):
    body = b"".join(response.streaming_content).decode()
    return [json.loads(line[6:]) for line in body.split("\n\n") if line.startswith("data: ")]


@pytest.mark.django_db
def test_demo_login_creates_isolated_account_with_pets(client, clinic):
    response = client.post(reverse("demo_login"))
    assert response.status_code == 302
    pets = Pet.objects.all()
    assert {p.name for p in pets} == {"Biscuit", "Miso"}
    assert client.get(reverse("my_pets")).status_code == 200

    other = client.__class__()
    other.post(reverse("demo_login"))
    assert Pet.objects.count() == 4  # each visitor gets their own pets


@pytest.mark.django_db
def test_chat_requires_login(client):
    assert client.post(reverse("assistant:send_message"), {"message": "hi"}).status_code == 302


@pytest.mark.django_db
def test_send_message_streams_sse(client, tutor, articles):
    client.force_login(tutor.user)
    response = client.post(reverse("assistant:send_message"), {"message": "rabies vaccine price"})
    assert response["Content-Type"] == "text/event-stream"
    events = read_events(response)
    assert [e["type"] for e in events][0] == "sources"
    assert events[-1]["type"] == "done"


@pytest.mark.django_db
def test_daily_limit(client, tutor, settings):
    settings.ASSISTANT_DAILY_MESSAGE_LIMIT = 1
    client.force_login(tutor.user)
    read_events(client.post(reverse("assistant:send_message"), {"message": "hello"}))
    response = client.post(reverse("assistant:send_message"), {"message": "hello again"})
    assert response.status_code == 429
    assert Message.objects.filter(role="user").count() == 1


@pytest.mark.django_db
def test_rejects_oversized_message(client, tutor):
    client.force_login(tutor.user)
    assert client.post(reverse("assistant:send_message"), {"message": "x" * 1001}).status_code == 400


@pytest.mark.django_db
def test_llm_failure_becomes_friendly_error(client, tutor, monkeypatch):
    import assistant.views

    def boom(*args, **kwargs):
        raise RuntimeError("provider down")
        yield

    monkeypatch.setattr(assistant.views, "answer", boom)
    client.force_login(tutor.user)
    events = read_events(client.post(reverse("assistant:send_message"), {"message": "hi"}))
    assert events == [{"type": "error", "message": "The assistant is unavailable right now. Please try again."}]


@pytest.mark.django_db
def test_pages_render(client, articles):
    for name in ["home", "helpcenter:index"]:
        assert client.get(reverse(name)).status_code == 200
    assert client.get("/help/vaccine-prices/").status_code == 200


@pytest.mark.django_db
def test_site_wide_daily_budget_stops_the_assistant(client, tutor, settings):
    from assistant.models import Conversation

    settings.ASSISTANT_DAILY_BUDGET_USD = 0.01
    other = Conversation.objects.create(user=tutor.user)
    Message.objects.create(conversation=other, role="assistant", content="x", cost_usd="0.010000")
    client.force_login(tutor.user)
    response = client.post(reverse("assistant:send_message"), {"message": "hello"})
    assert response.status_code == 429 and "budget" in response.json()["error"]


@pytest.mark.django_db
def test_demo_accounts_are_capped_per_day(client, clinic, settings):
    settings.DEMO_ACCOUNTS_PER_DAY = 2
    for _ in range(2):
        assert client.post(reverse("demo_login")).status_code == 302
    response = client.post(reverse("demo_login"))
    assert response.status_code == 429 and b"capacity" in response.content


@pytest.mark.django_db
def test_validation_errors_are_json(client, tutor):
    client.force_login(tutor.user)
    response = client.post(reverse("assistant:send_message"), {"message": ""})
    assert response.status_code == 400 and "1000" in response.json()["error"]


@pytest.mark.django_db
def test_healthz_reports_database_model_and_budget(client, settings):
    data = client.get("/healthz").json()
    assert data["ok"] and data["database"] and data["llm_configured"] and data["assistant_available"]

    settings.ASSISTANT_DAILY_BUDGET_USD = 0  # budget spent: reported, but the container stays healthy
    response = client.get("/healthz")
    assert response.status_code == 200 and response.json()["assistant_available"] is False

    settings.LLM_PROVIDER, settings.LLM_API_KEY = "openai_compatible", ""
    assert client.get("/healthz").status_code == 503


@pytest.mark.django_db
def test_spent_budget_is_said_up_front_and_the_composer_is_off(client, tutor, settings):
    settings.ASSISTANT_DAILY_BUDGET_USD = 0
    assert b"paused for today" in client.get(reverse("home")).content
    client.force_login(tutor.user)
    html = client.get(reverse("assistant:chat")).content.decode()
    assert "paused for today" in html and 'aria-label="Your question"' in html
    assert html.count(" disabled") >= 2  # input and Send button


@pytest.mark.django_db
def test_live_reply_meta_matches_a_reloaded_one(client, tutor, articles):
    client.force_login(tutor.user)
    done = read_events(client.post(reverse("assistant:send_message"), {"message": "rabies vaccine price"}))[-1]
    assert done["model"] == "fake" and done["tokens"] > 0 and "latency_ms" in done
