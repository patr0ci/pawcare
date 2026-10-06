import pytest

from assistant.chat import answer
from assistant.models import Conversation, Message
from assistant.retrieval import retrieve
from helpcenter.chunking import split_into_chunks
from helpcenter.ingest import ingest_directory


def test_chunks_carry_the_article_title():
    body = "\n\n".join(f"Paragraph {i} " + "x" * 300 for i in range(4))
    chunks = split_into_chunks("Vaccine Prices", body, max_chars=700)
    assert len(chunks) == 2
    assert all(chunk.startswith("Vaccine Prices\n\n") for chunk in chunks)


@pytest.mark.django_db
def test_real_content_ingests():
    articles, chunks = ingest_directory()
    assert articles >= 15
    assert chunks >= articles


@pytest.mark.django_db
def test_retrieve_ranks_the_relevant_article_first(articles):
    sources = retrieve("how much is the rabies vaccine")
    assert sources[0].title == "Vaccine Prices"
    assert sources[0].url == "/help/vaccine-prices/"
    assert [s.number for s in sources] == list(range(1, len(sources) + 1))


@pytest.mark.django_db
def test_retrieve_drops_irrelevant_chunks(articles, settings):
    assert retrieve("quantum chromodynamics lecture notes", max_distance=0.5) == []


@pytest.mark.django_db
def test_answer_streams_cites_and_logs_cost(articles, tutor):
    conversation = Conversation.objects.create(user=tutor.user)
    events = list(answer(conversation, "how much is the rabies vaccine"))

    assert events[0]["type"] == "sources"
    assert events[0]["sources"][0]["title"] == "Vaccine Prices"
    text = "".join(e["text"] for e in events if e["type"] == "delta")
    assert "$28" in text and "[1]" in text
    assert events[-1]["type"] == "done"

    user_msg, reply = conversation.messages.all()
    assert user_msg.role == Message.Role.USER
    assert reply.role == Message.Role.ASSISTANT
    assert reply.prompt_tokens > 0 and reply.cost_usd > 0
    assert reply.sources[0]["title"] == "Vaccine Prices"


@pytest.mark.django_db
def test_answer_admits_not_knowing_without_sources(tutor):
    conversation = Conversation.objects.create(user=tutor.user)
    events = list(answer(conversation, "what is the capital of France"))
    assert events[0] == {"type": "sources", "sources": []}
    text = "".join(e["text"] for e in events if e["type"] == "delta")
    assert "don't know" in text


@pytest.mark.django_db
def test_follow_up_questions_reuse_previous_question_for_retrieval(articles, tutor):
    conversation = Conversation.objects.create(user=tutor.user)
    list(answer(conversation, "how much is the rabies vaccine"))
    events = list(answer(conversation, "and DHPP?"))
    assert events[0]["sources"][0]["title"] == "Vaccine Prices"


@pytest.mark.django_db
def test_chunks_of_the_same_article_become_one_source(db):
    from helpcenter.ingest import index_article
    from helpcenter.models import Article

    body = "\n\n".join(f"Rabies vaccine fact number {i}. " + "filler " * 120 for i in range(3))
    index_article(Article.objects.create(slug="rabies", title="Rabies", category="Vaccines", body=body))
    sources = retrieve("rabies vaccine fact", max_distance=2)
    assert len(sources) == 1
    assert sources[0].text.count("Rabies vaccine fact number") == 3
