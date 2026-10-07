import pytest

from assistant.embeddings import get_embedder
from assistant.llm import get_llm
from clinic.demo import create_demo_tutor, seed_clinic
from helpcenter.models import Article


@pytest.fixture(autouse=True)
def offline_ai(settings):
    """Tests never download models or call a paid API."""
    settings.EMBEDDING_PROVIDER = "fake"
    settings.LLM_PROVIDER = "fake"
    settings.RAG_MAX_DISTANCE = 0.8
    get_embedder.cache_clear()
    get_llm.cache_clear()
    yield
    get_embedder.cache_clear()
    get_llm.cache_clear()


@pytest.fixture
def clinic(db):
    seed_clinic()


@pytest.fixture
def tutor(db, clinic):
    return create_demo_tutor()


@pytest.fixture
def articles(db):
    from helpcenter.ingest import index_article

    data = [
        (
            "vaccine-prices",
            "Vaccine Prices",
            "Vaccines & Prevention",
            "Rabies vaccine costs $28. DHPP costs $35.\n\nThe vaccination visit fee is $45.",
        ),
        (
            "opening-hours",
            "Opening Hours",
            "Visits & Booking",
            "We are open Monday to Friday from 9 to 18 and Saturday from 9 to 13. Closed on Sunday.",
        ),
        (
            "emergencies",
            "Emergencies",
            "Emergencies",
            "After hours call the Springfield 24h Animal ER. Lilies are toxic to cats.",
        ),
    ]
    for slug, title, category, body in data:
        index_article(Article.objects.create(slug=slug, title=title, category=category, body=body))
