"""Regression tests for the second QA round (site: pages, admin, help center, booking policy, settings)."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from django.core.management import call_command
from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone
from django.views.defaults import server_error

import helpcenter.ingest
from clinic.demo import create_demo_tutor
from clinic.models import Appointment, Service, Tutor, Vet
from clinic.services import late_change_fee
from helpcenter.ingest import ingest_directory
from helpcenter.models import Article
from tests.test_qa_fixes import at
from tests.test_scheduling import next_weekday


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
    write_article(tmp_path, "good")
    write_article(tmp_path, name, title="Bad name")
    stats = ingest_directory(tmp_path)
    assert (stats["created"], stats["errors"]) == (1, 1)
    assert client.get("/help/").status_code == 200


@pytest.mark.django_db
def test_an_article_left_with_a_bad_slug_is_removed(client, tmp_path):
    Article.objects.create(slug="dotted.name", title="Old", category="X", body="Old")  # from an older ingest
    write_article(tmp_path, "dotted.name")
    assert ingest_directory(tmp_path)["deleted"] == 1
    assert client.get("/help/").status_code == 200


@pytest.mark.django_db
def test_a_title_too_long_for_its_column_is_skipped(tmp_path):
    write_article(tmp_path, "long-title", title="t" * 201)
    assert ingest_directory(tmp_path)["errors"] == 1


@pytest.mark.django_db
def test_an_interrupted_update_leaves_the_old_text_and_chunks_together(tmp_path, monkeypatch):
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
    call_command("seed_clinic")
    assert Vet.objects.count() == 3 and Service.objects.count() == 7
    assert Vet.objects.get(name="Dr. Rafael Souza").services.filter(name="Dental cleaning").exists()


def local(day, hour, minute=0):
    """A time at the clinic in the week of Monday 2030-01-07."""
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
    assert late_change_fee(Appointment(starts_at=starts_at), now=requested) == fee


def test_completed_visits_never_have_a_late_change_fee():
    done = Appointment(starts_at=local(7, 10), status=Appointment.Status.COMPLETED)
    assert late_change_fee(done, now=local(7, 9)) == 0


@pytest.mark.django_db
def test_tutors_are_told_apart_by_username(tutor):
    assert str(tutor) == f"Demo Visitor ({tutor.user.username})"


@pytest.mark.django_db
def test_admin_lists_are_searchable_by_username_without_a_query_per_row(admin_client, clinic):
    def queries(url):
        with CaptureQueriesContext(connection) as captured:
            assert admin_client.get(url).status_code == 200
        return len(captured)

    first = create_demo_tutor()
    Appointment.objects.create(
        pet=first.pets.first(), vet=Vet.objects.first(), service=Service.objects.first(), starts_at=timezone.now()
    )
    urls = [reverse(f"admin:clinic_{model}_changelist") for model in ("tutor", "pet", "appointment")]
    before = [queries(url) for url in urls]
    for _ in range(4):
        tutor = create_demo_tutor()
        Appointment.objects.create(
            pet=tutor.pets.first(), vet=Vet.objects.first(), service=Service.objects.first(), starts_at=timezone.now()
        )
    assert [queries(url) for url in urls] == before

    assert "1 result" in admin_client.get(urls[0], {"q": first.user.username}).content.decode()
    html = admin_client.get(urls[1], {"q": first.user.username}).content.decode()
    assert "2 results" in html and first.user.username in html


@pytest.mark.django_db
def test_tutor_page_cant_delete_pets_or_overflow_with_allergies(admin_client, tutor):
    html = admin_client.get(reverse("admin:clinic_tutor_change", args=[tutor.id])).content.decode()
    assert 'name="pets-0-name"' in html
    assert 'name="pets-0-DELETE"' not in html and 'name="pets-0-allergies"' not in html


def admin_form(pet, vet, service, starts_at, **extra):
    when = timezone.localtime(starts_at)
    return {
        "pet": pet.id,
        "vet": vet.id,
        "service": service.id,
        "starts_at_0": when.strftime("%Y-%m-%d"),
        "starts_at_1": when.strftime("%H:%M"),
        "status": "scheduled",
        "notes": "",
        "cancellation_reason": "",
        **extra,
    }


@pytest.mark.django_db
def test_admin_bookings_follow_the_booking_rules(admin_client, tutor):
    biscuit, miso = tutor.pets.get(name="Biscuit"), tutor.pets.get(name="Miso")
    exam, chen = Service.objects.get(name="Wellness exam"), Vet.objects.get(name="Dr. Maya Chen")
    day = next_weekday(2)
    add = reverse("admin:clinic_appointment_add")

    assert admin_client.post(add, admin_form(biscuit, chen, exam, at(day, 10))).status_code == 302
    html = admin_client.post(add, admin_form(miso, chen, exam, at(day, 10))).content.decode()  # vet already busy
    assert "no longer available" in html
    html = admin_client.post(add, admin_form(miso, Vet.objects.get(name="Dr. Rafael Souza"), exam, at(day, 11)))
    assert "doesn&#x27;t do wellness exam" in html.content.decode()
    html = admin_client.post(add, admin_form(miso, chen, exam, at(next_weekday(6), 10))).content.decode()  # Sunday
    assert "no longer available" in html
    assert Appointment.objects.count() == 1


@pytest.mark.django_db
def test_admin_checks_a_revived_visit_but_not_notes_on_a_past_one(admin_client, tutor):
    biscuit, miso = tutor.pets.get(name="Biscuit"), tutor.pets.get(name="Miso")
    exam, chen = Service.objects.get(name="Wellness exam"), Vet.objects.get(name="Dr. Maya Chen")
    eleven = at(next_weekday(2), 11)
    cancelled = Appointment.objects.create(
        pet=miso, vet=chen, service=exam, starts_at=eleven, status=Appointment.Status.CANCELLED
    )
    Appointment.objects.create(pet=biscuit, vet=chen, service=exam, starts_at=eleven)  # took the freed slot
    change = reverse("admin:clinic_appointment_change", args=[cancelled.id])
    assert "no longer available" in admin_client.post(change, admin_form(miso, chen, exam, eleven)).content.decode()

    past = Appointment.objects.create(pet=miso, vet=chen, service=exam, starts_at=timezone.now() - timedelta(days=2))
    change = reverse("admin:clinic_appointment_change", args=[past.id])
    data = admin_form(miso, chen, exam, past.starts_at, status="completed", notes="All good.")
    assert admin_client.post(change, data).status_code == 302
    past.refresh_from_db()
    assert (past.status, past.notes) == ("completed", "All good.")


@pytest.mark.django_db
def test_appointment_str_uses_clinic_time(tutor):
    appointment = Appointment(
        pet=tutor.pets.get(name="Biscuit"),
        vet=Vet.objects.first(),
        service=Service.objects.get(name="Nail trim"),
        starts_at=datetime(2030, 1, 8, 15, 0, tzinfo=UTC),  # 10:00 in New York
    )
    assert str(appointment).endswith("2030-01-08 10:00")


@pytest.mark.django_db
def test_error_pages_carry_the_site_and_a_way_home(client, rf):
    response = client.get("/no-such-page/")
    html = response.content.decode()
    assert response.status_code == 404 and "couldn't find that page" in html and 'href="/"' in html
    response = server_error(rf.get("/"))  # rendered with no context at all, as Django does on a 500
    html = response.content.decode()
    assert response.status_code == 500 and "PawCare" in html and 'href="/"' in html


def test_favicon_ico_points_at_the_svg(client):
    response = client.get("/favicon.ico")
    assert response.status_code == 302 and response.url.endswith("img/favicon.svg")


@pytest.mark.django_db
def test_opening_demo_link_goes_home_and_only_post_creates_an_account(client, clinic):
    response = client.get(reverse("demo_login"))
    assert response.status_code == 302 and response.url == reverse("home")
    assert not Tutor.objects.exists() and "_auth_user_id" not in client.session
    assert client.post(reverse("demo_login")).status_code == 302 and Tutor.objects.count() == 1


@pytest.mark.django_db
def test_help_articles_describe_themselves_in_link_previews(client):
    body = "We know plans change.\n\n- Free with 24 hours' notice\n- $25 otherwise " + "and more " * 40
    Article.objects.create(slug="policy", title="Cancellation & No-Show", category="Policies", body=body)
    html = client.get("/help/policy/").content.decode()
    assert '<meta property="og:title" content="Cancellation &amp; No-Show · PawCare Help Center">' in html
    expected = "We know plans change. - Free with 24 hours&#x27; notice - $25 otherwise and more"
    assert f'<meta name="description" content="{expected}' in html
    assert f'<meta property="og:description" content="{expected}' in html
    assert "an AI assistant added to an existing Django app" in client.get("/").content.decode()  # the default


@pytest.mark.django_db
def test_my_pets_after_a_cancellation_says_no_upcoming_appointments(client, tutor):
    Appointment.objects.create(
        pet=tutor.pets.first(),
        vet=Vet.objects.first(),
        service=Service.objects.first(),
        starts_at=at(next_weekday(2), 10),
        status=Appointment.Status.CANCELLED,
    )
    client.force_login(tutor.user)
    html = client.get(reverse("my_pets")).content.decode()
    assert "No upcoming appointments." in html and reverse("assistant:chat") in html
