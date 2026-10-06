import json

import pytest
from django.contrib.auth import get_user_model
from django.urls import reverse

from assistant.evals.runner import run_evals, score_case
from assistant.models import Conversation, EvalRun, Message


def test_score_case_checks():
    case = {"must_include": ["$28"], "must_cite": "vaccine-prices"}
    assert score_case(case, "Rabies is $28 [1].", ["vaccine-prices"]) == []
    assert score_case(case, "Rabies is cheap.", ["other"]) == ["missing “$28”", "did not cite vaccine-prices"]
    assert score_case({"must_cite": ["a", "b"]}, "x", ["b"]) == []
    assert score_case({"must_refuse": True}, "Paris.", []) == ["should have said it doesn't know"]
    assert score_case({"must_refuse": True}, "I don't know, please call us.", []) == []
    assert score_case({"must_not_include": ["mg"]}, "Give 50 mg.", []) == ["should not say “mg”"]


@pytest.mark.django_db
def test_run_evals_scores_and_leaves_no_conversations(articles, tmp_path):
    cases = tmp_path / "cases.json"
    cases.write_text(json.dumps([
        {"question": "how much is the rabies vaccine", "must_include": ["$28"], "must_cite": "vaccine-prices"},
        {"question": "capital of france", "must_refuse": True},
        {"question": "how much is the rabies vaccine", "must_include": ["$999"]},
    ]))
    run = run_evals(cases)
    assert (run.total, run.passed) == (3, 2)
    assert run.results[2]["failures"] == ["missing “$999”"]
    assert not Conversation.objects.exists()  # eval traffic doesn't pollute real usage stats


@pytest.mark.django_db
def test_dashboard_is_staff_only_and_renders(client, tutor, articles):
    conversation = Conversation.objects.create(user=tutor.user)
    Message.objects.create(conversation=conversation, role="assistant", content="hi", model="m",
                           prompt_tokens=10, completion_tokens=5, cost_usd="0.000100",
                           tool_calls=[{"name": "list_my_pets", "arguments": "{}", "result": {}}])
    EvalRun.objects.create(model="m", total=1, passed=1, results=[{"question": "q", "answer": "a", "cited": [], "failures": []}])

    client.force_login(tutor.user)
    assert client.get(reverse("assistant:dashboard")).status_code == 302  # not staff

    staff = get_user_model().objects.create_user("staff", password="x", is_staff=True)
    client.force_login(staff)
    html = client.get(reverse("assistant:dashboard")).content.decode()
    assert "list_my_pets" in html and "1/1" in html and "$0.0001" in html


@pytest.mark.django_db
def test_public_demo_dashboard(client, tutor, settings):
    settings.DEMO_PUBLIC_DASHBOARD = True
    client.force_login(tutor.user)
    assert client.get(reverse("assistant:dashboard")).status_code == 200
    client.logout()
    assert client.get(reverse("assistant:dashboard")).status_code == 302
