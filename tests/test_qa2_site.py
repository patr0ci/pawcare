"""Regression tests for the second QA round (site: pages, admin, help center, booking policy, settings)."""

import pytest
from django.urls import reverse


@pytest.mark.django_db
def test_only_login_and_logout_are_public_account_pages(client):
    assert client.get("/accounts/password_reset/").status_code == 404
    assert client.get("/accounts/password_change/").status_code == 404
    assert client.get(reverse("login")).status_code == 200


@pytest.mark.django_db
def test_login_page_sends_a_logged_in_visitor_on(client, tutor):
    client.force_login(tutor.user)
    response = client.get(reverse("login"))
    assert response.status_code == 302 and response.url == reverse("home")
    assert client.get(reverse("login") + "?next=/help/").url == "/help/"
