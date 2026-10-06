"""RAG answer pipeline: retrieve help-center chunks, ground the model on them, stream the answer, log usage."""

import time
from collections.abc import Iterator
from dataclasses import asdict

from .llm import Delta, Done, get_llm
from .models import Conversation, Message
from .retrieval import Source, retrieve

HISTORY_TURNS = 6

SYSTEM_PROMPT = """You are the virtual assistant of PawCare Veterinary Clinic.
Answer the client's question using ONLY the numbered sources below.

Rules:
- Cite the sources you used inline, like [1] or [2]. Every factual sentence needs a citation.
- If the sources don't contain the answer, say you don't know and suggest calling the clinic at (555) 014-7788. Never invent prices, dates, or policies.
- You are not a veterinarian: do not diagnose or prescribe. For anything that sounds urgent, point to the emergency information in the sources.
- Judge every question on its own against the sources below; earlier refusals in the conversation don't carry over.
- If something is only partly covered (e.g. a rule with conditions), explain the condition instead of refusing.
- Be brief and friendly. Plain text, no markdown headings.

Sources:
{sources}"""


def format_sources(sources: list[Source]) -> str:
    if not sources:
        return "(no relevant sources found)"
    return "\n\n".join(f"[{s.number}] {s.title} ({s.url})\n{s.text}" for s in sources)


def build_messages(conversation: Conversation, question: str, sources: list[Source]) -> list[dict]:
    history = list(conversation.messages.order_by("-created_at")[:HISTORY_TURNS])[::-1]
    return [
        {"role": "system", "content": SYSTEM_PROMPT.format(sources=format_sources(sources))},
        *({"role": m.role, "content": m.content} for m in history),
        {"role": "user", "content": question},
    ]


def retrieval_queries(conversation: Conversation, question: str) -> list[str]:
    """The question alone, plus the question joined to the previous one. The second catches follow-ups
    like "and for cats?"; keeping the first means a change of topic isn't dragged back to the old one."""
    previous = conversation.messages.filter(role=Message.Role.USER).order_by("-created_at").first()
    return [question, f"{previous.content}\n{question}"] if previous else [question]


def answer(conversation: Conversation, question: str) -> Iterator[dict]:
    """Yields SSE-ready events: {"type": "delta"|"sources"|"done", ...}. Persists both messages."""
    started = time.monotonic()
    sources = retrieve(retrieval_queries(conversation, question))
    messages = build_messages(conversation, question, sources)
    Message.objects.create(conversation=conversation, role=Message.Role.USER, content=question)

    yield {"type": "sources", "sources": [{"number": s.number, "title": s.title, "url": s.url} for s in sources]}

    parts: list[str] = []
    for event in get_llm().stream(messages):
        if isinstance(event, Delta):
            parts.append(event.text)
            yield {"type": "delta", "text": event.text}
        elif isinstance(event, Done):
            reply = Message.objects.create(
                conversation=conversation,
                role=Message.Role.ASSISTANT,
                content="".join(parts).strip(),
                sources=[asdict(s) | {"text": s.text[:300]} for s in sources],
                model=event.model,
                prompt_tokens=event.usage.prompt_tokens,
                completion_tokens=event.usage.completion_tokens,
                cost_usd=event.usage.cost_usd,
                latency_ms=int((time.monotonic() - started) * 1000),
            )
            yield {"type": "done", "message_id": reply.id, "cost_usd": float(reply.cost_usd)}
