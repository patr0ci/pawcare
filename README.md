# PawCare — adding an AI assistant to an existing Django app

A fictional vet clinic app (pets, vets, services, appointments, help center) with an AI assistant layered on top.
It's a portfolio demo of the kind of work I do for clients: **adding LLM features to a Django product that already exists**,
in a way the team can test, monitor and pay for predictably.

## What the assistant does

- **Answers from the clinic's own content (RAG).** Help-center articles are chunked, embedded locally
  (`fastembed`, `bge-small-en-v1.5` — no API cost for embeddings) and stored in Postgres with **pgvector** (HNSW index).
- **Cites its sources.** Retrieved chunks are grouped per article and numbered; the model must cite `[n]` and the UI links each one.
- **Says "I don't know".** Chunks beyond a cosine-distance threshold are dropped, and the prompt forbids inventing prices or policies.
- **Streams** the answer token by token (Server-Sent Events over a plain Django `StreamingHttpResponse`).
- **Tracks cost.** Every reply stores model, prompt/completion tokens, USD cost and latency; each user has a daily message cap.
- **Works with any OpenAI-compatible provider** — OpenRouter, OpenAI, DeepSeek, or a self-hosted model — by changing env vars.
  `LLM_PROVIDER=fake` runs everything offline.

## Architecture

```
clinic/       the "existing system": Tutor, Pet, Vet, Service, Appointment + slot availability
helpcenter/   Article + Chunk(embedding vector(384)); markdown content; ingestion command
assistant/    embeddings.py · retrieval.py · llm.py (provider-agnostic, streaming) · chat.py (RAG pipeline) · views (SSE)
```

Request flow: question → (previous question appended for follow-ups) → embed → pgvector top-k with distance cutoff →
group by article → grounded system prompt with numbered sources → stream from LLM → persist message + usage.

## Run it locally

```bash
docker compose up -d                 # Postgres 17 + pgvector on localhost:5434
cp .env.example .env                 # add LLM_API_KEY, or set LLM_PROVIDER=fake
uv sync
uv run python manage.py migrate
uv run python manage.py seed_clinic
uv run python manage.py ingest_helpcenter
uv run python manage.py runserver
```

Open http://localhost:8000 and click **Try it** — you get a throwaway account with two pets.

## Tests

```bash
uv run pytest
```

Tests run fully offline (hashing embedder + fake LLM) but exercise real pgvector queries, the streaming endpoint,
the daily limit, error handling and appointment-slot logic.

## Roadmap

- [x] RAG over the help center with citations, streaming, cost logging, daily limits
- [ ] Tool-calling agent: check availability, book / reschedule / cancel — with an explicit confirmation step before any write
- [ ] Cost & usage dashboard for staff
- [ ] Evaluation page: a fixed question set scored for answer accuracy and citation correctness
- [ ] Docker image + Caddy deploy

All clinic data, people and prices are fictional.
