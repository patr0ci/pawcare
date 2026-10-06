# PawCare — adding an AI assistant to an existing Django app

A fictional vet clinic app (pets, vets, services, appointments, help center) with an AI assistant layered on top.
It's a portfolio demo of the kind of work I do for clients: **adding LLM features to a Django product that already exists**,
in a way the team can test, monitor and pay for predictably.

## What the assistant does

- **Answers from the clinic's own content (RAG).** Help-center articles are chunked, embedded locally
  (`fastembed`, `bge-small-en-v1.5` — no API cost for embeddings) and stored in Postgres with **pgvector** (HNSW index).
- **Cites its sources.** Retrieved chunks are grouped per article and numbered; the model must cite `[n]` and the UI links each one.
- **Says "I don't know".** Chunks beyond a cosine-distance threshold are dropped, and the prompt forbids inventing prices or policies.
- **Acts on the client's account with tools** — lists pets and appointments, finds free slots, and proposes
  bookings, reschedules and cancellations. **The model can't write anything**: a write tool only creates a
  `PendingAction`; the client sees a card and clicks **Confirm**, and the change then runs through the clinic's own
  booking rules (`clinic/services.py`) — ownership checks, opening hours, vet/species match, slot still free,
  24-hour late-change fee. Proposals expire after 30 minutes and can't be replayed.
- **Tools are scoped to the logged-in user.** The model never passes a user id, so it can't reach another client's data.
- **Streams** the answer token by token (Server-Sent Events over a plain Django `StreamingHttpResponse`).
- **Tracks cost.** Every reply stores model, prompt/completion tokens, USD cost and latency; each user has a daily message cap.
- **Works with any OpenAI-compatible provider** — OpenRouter, OpenAI, DeepSeek, or a self-hosted model — by changing env vars.
  `LLM_PROVIDER=fake` runs everything offline.

## Architecture

```
clinic/       the "existing system": Tutor, Pet, Vet, Service, Appointment + slot availability
helpcenter/   Article + Chunk(embedding vector(384)); markdown content; ingestion command
clinic/services.py   booking rules shared by the UI and the assistant (book / reschedule / cancel)
assistant/    embeddings.py · retrieval.py · llm.py (provider-agnostic, streaming + tool calls)
              tools.py (tool schemas, read tools, propose-only write tools) · chat.py (RAG + tool loop) · views (SSE, confirm)
```

Request flow: question → retrieve (current question first, previous one only to fill follow-ups) → pgvector top-k
with distance cutoff → group by article → system prompt with numbered sources, today's date and a 14-day calendar →
tool loop (max 6 rounds) → stream → keep only the sources actually cited → persist message, usage and tool trace.

## Choosing the model

Same scripted conversation (book, list, cancel, mixed question) against several models via OpenRouter, Oct 2026:

| Model | Booked | Cancelled | ~Cost / turn |
|---|---|---|---|
| Gemini 2.5 Flash-Lite | asked for the date again | no | $0.0003 |
| DeepSeek V4 Flash | yes | claimed a proposal it never made | $0.0006 |
| **DeepSeek V4.1 Flash** (default) | yes | yes | **$0.0008** |
| Gemini 2.5 Flash | yes, in one turn | guessed an id | $0.0015 |
| GPT-5 mini | yes | yes | $0.0030 |

Two prompt changes mattered more than the model for the weaker ones: spelling out the next 14 dates (small models are
bad at "next Tuesday") and telling the model to retry once with a looked-up id when a tool returns an error.

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

Tests run fully offline (hashing embedder + fake/scripted LLM) but exercise real pgvector queries, the streaming
endpoint, the daily limit, error handling, slot logic, and the agent's safety rules: nothing is written before
Confirm, tools can't reach other clients' pets, a slot taken between proposal and confirmation fails cleanly,
proposals expire and can't be replayed, runaway tool loops are cut off, and streamed tool-call fragments are reassembled.

## Roadmap

- [x] RAG over the help center with citations, streaming, cost logging, daily limits
- [x] Tool-calling agent: check availability, book / reschedule / cancel — with an explicit confirmation step before any write
- [ ] Cost & usage dashboard for staff
- [ ] Evaluation page: a fixed question set scored for answer accuracy and citation correctness
- [ ] Docker image + Caddy deploy

All clinic data, people and prices are fictional.
