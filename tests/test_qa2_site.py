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


def write_article(directory, name, title="Good", body="Body"):
    (directory / f"{name}.md").write_text(f"---\ntitle: {title}\ncategory: X\n---\n{body}\n", encoding="utf-8")


@pytest.mark.django_db
@pytest.mark.parametrize("name", ["x" * 51, "has space", "dotted.name", "café"])
def test_file_names_that_cant_be_slugs_are_skipped_not_fatal(client, tmp_path, name):
    from helpcenter.ingest import ingest_directory

    write_article(tmp_path, "good")
    write_article(tmp_path, name, title="Bad name")
    stats = ingest_directory(tmp_path)
    assert (stats["created"], stats["errors"]) == (1, 1)
    assert client.get("/help/").status_code == 200


@pytest.mark.django_db
def test_an_article_left_with_a_bad_slug_is_removed(client, tmp_path):
    from helpcenter.ingest import ingest_directory
    from helpcenter.models import Article

    Article.objects.create(slug="dotted.name", title="Old", category="X", body="Old")  # from an older ingest
    write_article(tmp_path, "dotted.name")
    assert ingest_directory(tmp_path)["deleted"] == 1
    assert client.get("/help/").status_code == 200


@pytest.mark.django_db
def test_a_title_too_long_for_its_column_is_skipped(tmp_path):
    from helpcenter.ingest import ingest_directory

    write_article(tmp_path, "long-title", title="t" * 201)
    assert ingest_directory(tmp_path)["errors"] == 1


@pytest.mark.django_db
def test_an_interrupted_update_leaves_the_old_text_and_chunks_together(tmp_path, monkeypatch):
    import helpcenter.ingest
    from helpcenter.ingest import ingest_directory
    from helpcenter.models import Article

    write_article(tmp_path, "fees", body="Late fee is $25.")
    ingest_directory(tmp_path)
    write_article(tmp_path, "fees", body="Late fee is $30.")

    def interrupted(article):
        raise KeyboardInterrupt

    monkeypatch.setattr(helpcenter.ingest, "index_article", interrupted)
    with pytest.raises(KeyboardInterrupt):
        ingest_directory(tmp_path)
    article = Article.objects.get()
    assert article.body == "Late fee is $25." and "$25" in article.chunks.get().text

    monkeypatch.undo()
    assert ingest_directory(tmp_path)["updated"] == 1  # not "unchanged" with the old chunks
    assert "$30" in Article.objects.get().chunks.get().text


@pytest.mark.django_db
def test_cross_references_link_titles_with_ampersands_and_apostrophes(client):
    from helpcenter.models import Article

    Article.objects.create(slug="fleas-ticks", title="Fleas & Ticks", category="V", body="x")
    Article.objects.create(slug="whats-covered", title="What's Covered", category="V", body="x")
    Article.objects.create(
        slug="plans",
        title="Plans",
        category="P",
        body='See "Fleas & Ticks" and "What\'s Covered".\n- Not a title: "Cats & <b>Dogs</b>"',
    )
    html = client.get("/help/plans/").content.decode()
    assert '"<a href="/help/fleas-ticks/">Fleas &amp; Ticks</a>"' in html
    assert '"<a href="/help/whats-covered/">What&#x27;s Covered</a>"' in html
    assert "<li>Not a title: &quot;Cats &amp; &lt;b&gt;Dogs&lt;/b&gt;&quot;</li>" in html


@pytest.mark.django_db
def test_seed_on_boot_keeps_admin_edits_and_force_resyncs(clinic):
    from decimal import Decimal

    from django.core.management import call_command

    from clinic.models import Service, Vet

    Service.objects.filter(name="Nail trim").update(price_usd=Decimal("22.00"), duration_minutes=20)
    Vet.objects.filter(name="Dr. Maya Chen").update(name="Dr. Maya Chen-Park")
    call_command("seed_clinic")  # what every boot runs
    nail_trim = Service.objects.get(name="Nail trim")
    assert (nail_trim.price_usd, nail_trim.duration_minutes) == (Decimal("22.00"), 20)
    assert Vet.objects.count() == 3 and not Vet.objects.filter(name="Dr. Maya Chen").exists()

    call_command("seed_clinic", force=True)
    assert Service.objects.get(name="Nail trim").price_usd == Decimal("20.00")
    assert Vet.objects.filter(name="Dr. Maya Chen").exists()


@pytest.mark.django_db
def test_seed_fills_an_empty_clinic():
    from django.core.management import call_command

    from clinic.models import Service, Vet

    call_command("seed_clinic")
    assert Vet.objects.count() == 3 and Service.objects.count() == 7
    assert Vet.objects.get(name="Dr. Rafael Souza").services.filter(name="Dental cleaning").exists()


def local(day, hour, minute=0):
    """A time at the clinic in the week of Monday 2030-01-07."""
    from datetime import datetime

    from django.utils import timezone

    return timezone.make_aware(datetime(2030, 1, day, hour, minute))


@pytest.mark.parametrize(
    "requested, starts_at, fee",
    [
        (local(6, 9), local(7, 10), 25),  # Sunday for Monday: due by Saturday 10:00, and it's read on Monday
        (local(5, 9, 30), local(7, 10), 0),  # Saturday morning for Monday, before Saturday 10:00
        (local(5, 10, 30), local(7, 10), 25),  # Saturday, but past the Saturday 10:00 deadline
        (local(5, 14), local(7, 15), 25),  # Saturday after closing: received Monday 9:00, past Saturday 15:00
        (local(6, 20), local(8, 10), 0),  # Sunday night for Tuesday: received Monday 9:00, deadline Monday 10:00
        (local(9, 10), local(10, 10), 0),  # exactly 24 hours ahead, while open
        (local(9, 10, 1), local(10, 10), 25),
        (local(8, 19), local(10, 9, 30), 0),  # Tuesday evening: received Wednesday 9:00, deadline Wednesday 9:30
        (local(9, 19), local(10, 9, 30), 25),  # Wednesday evening for Thursday morning: under 24 hours
        (local(10, 11), local(10, 10), 0),  # already happened
    ],
)
def test_late_change_fee_follows_the_published_policy(requested, starts_at, fee):
    from clinic.models import Appointment
    from clinic.services import late_change_fee

    assert late_change_fee(Appointment(starts_at=starts_at), now=requested) == fee


def test_completed_visits_never_have_a_late_change_fee():
    from clinic.models import Appointment
    from clinic.services import late_change_fee

    done = Appointment(starts_at=local(7, 10), status=Appointment.Status.COMPLETED)
    assert late_change_fee(done, now=local(7, 9)) == 0
