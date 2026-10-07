"""Answer pipeline: retrieve help-center chunks (RAG), let the model call clinic tools, stream the answer, log usage.

Read tools run inline. Write tools only produce PendingActions that the user confirms in the UI (see tools.py)."""

import json
import re
import time
from collections.abc import Iterator
from dataclasses import asdict
from datetime import timedelta

from django.conf import settings
from django.utils import timezone

from .llm import Delta, Done, Usage, get_llm
from .models import Conversation, Message, PendingAction
from .retrieval import Source, retrieve
from .tools import STATUS_LABELS, TOOLS, ToolRunner

HISTORY_TURNS = 6
MAX_TOOL_ROUNDS = 6
ANSWER_DEADLINE_S = 90  # no new LLM round starts after this; each call also has its own timeout

# Seen on the public demo: the model wrote "I've set up a proposal, please confirm" without calling propose_booking,
# so there was no Confirm button. A reply that claims an action needs a proposal behind it; if there is none,
# the model gets one corrective round.
# First-person claims only: "your exam is booked for Tuesday" is a fact when it comes from list_my_appointments.
CLAIMS_ACTION = re.compile(
    r"\bproposal\b|\b(?:click|tap|press)\s+(?:the\s+)?confirm\b|\bconfirm button\b"
    r"|\bi(?:'ve| have)(?:\s+just)?\s+(?:booked|cancell?ed|rescheduled|moved|set up|prepared|proposed)\b"
    r"|\bproposta\b|\bclique em confirmar\b",  # the assistant answers in the client's language
    re.I,
)
UNBACKED_CLAIM_NUDGE = (
    "Automatic check (not from the client): your last reply talks about a booking, change or cancellation, but no "
    "propose_* tool was called, so the client has no Confirm button. If you have what you need, call the right "
    "propose_* tool now. Otherwise, correct your reply: ask for what's missing, and don't say anything is proposed or done."
)

SYSTEM_PROMPT = """You are the virtual assistant of PawCare Veterinary Clinic.
Today is {today} (clinic time zone). Upcoming dates: {calendar}.

For questions about the clinic (prices, policies, care), use ONLY the numbered sources below.
For the client's own pets and appointments, use the tools:
- Never guess ids. Call list_my_pets / list_services / list_my_appointments first, in the same turn.
- Don't narrate tool use ("let me look that up"); just answer once you have what you need.
- Resolve relative dates ("next Tuesday", "tomorrow") with the calendar above and always say the exact date you
  picked (e.g. "Thursday, Oct 15"). If the client gave a day
  (and maybe "morning"/"afternoon"), search right away and offer the earliest matching times; don't ask again.
- To book, reschedule or cancel: find a real free slot when needed, then call the matching propose_* tool in the
  same turn. Only the tool creates the Confirm button: never say you've set up a proposal unless it returned
  "awaiting_client_confirmation". A proposal is NOT done until the client clicks Confirm, so say "please confirm",
  never "booked" or "cancelled".
- If a tool returns an error, fix the call (e.g. look the id up) and retry once before giving up.
- Only ask a question when the pet or service is genuinely ambiguous.

Rules:
- Cite help-center sources inline, like [1] or [2]. Facts that came from tools (pets, slots, appointments) need no citation.
- If the sources don't contain the answer, say you don't know and suggest calling the clinic at (555) 014-7788. Never invent prices, dates, or policies.
- Only help with PawCare, pet care covered by the sources, and the client's own account. Politely decline anything
  else (general knowledge, coding, other businesses) without answering it.
- You are not a veterinarian: do not diagnose or prescribe. If it sounds urgent or after hours, give the emergency
  contact from the sources in full: name, address and phone number.
- Judge every question on its own against the sources below; earlier refusals in the conversation don't carry over.
- Don't retract or apologise for an earlier answer that the sources support.
- If something is only partly covered (e.g. a rule with conditions), explain the condition instead of refusing.
- Be brief and friendly. Plain text, no markdown headings.

Sources:
{sources}"""


def cited_numbers(text: str) -> set[int]:
    """Numbers inside citation brackets: "[1]", "[2, 3]", "[1][4]"."""
    return {int(n) for group in re.findall(r"\[(\d+(?:\s*,\s*\d+)*)\]", text) for n in re.findall(r"\d+", group)}


def is_unbacked_claim(conversation: Conversation, runner: ToolRunner, text: str) -> bool:
    """The reply talks about a proposal or a done change, but nothing was proposed this turn and no earlier
    proposal is still waiting for the client (pointing back at one of those is fine)."""
    if runner.proposed or not CLAIMS_ACTION.search(text):
        return False
    return not conversation.actions.filter(status=PendingAction.Status.PENDING).exists()


def upcoming_calendar(days: int = 14) -> str:
    """Small models are unreliable at date arithmetic; spelling out the next two weeks fixes "next Tuesday"."""
    today = timezone.localdate()
    return ", ".join(
        (today + timedelta(days=i)).strftime("%a %Y-%m-%d") for i in range(1, days + 1)
    )


def format_sources(sources: list[Source]) -> str:
    if not sources:
        return "(no relevant sources found)"
    return "\n\n".join(f"[{s.number}] {s.title} ({s.url})\n{s.text}" for s in sources)


def build_messages(conversation: Conversation, question: str, sources: list[Source]) -> list[dict]:
    history: list[dict] = []
    for m in list(conversation.messages.order_by("-created_at")[:HISTORY_TURNS])[::-1]:
        if history and history[-1]["role"] == m.role:
            history[-1]["content"] += "\n\n" + m.content  # some providers require alternating roles
        else:
            history.append({"role": m.role, "content": m.content})
    return [
        {
            "role": "system",
            "content": SYSTEM_PROMPT.format(
                today=timezone.localdate().strftime("%A, %Y-%m-%d"),
                calendar=upcoming_calendar(),
                sources=format_sources(sources),
            ),
        },
        *history,
        {"role": "user", "content": question},
    ]


def retrieval_queries(conversation: Conversation, question: str) -> list[str]:
    """The question alone, plus the question joined to the previous one. The second catches follow-ups
    like "and for cats?"; keeping the first means a change of topic isn't dragged back to the old one."""
    previous = conversation.messages.filter(role=Message.Role.USER).order_by("-created_at").first()
    return [question, f"{previous.content}\n{question}"] if previous else [question]


def answer(conversation: Conversation, question: str) -> Iterator[dict]:
    """Yields SSE-ready events: sources, tool (status), action (needs confirmation), delta, done.

    The reply (with usage and the tool trace) is saved even if the provider fails mid-way or the client
    disconnects, so the dashboard counts what was actually spent."""
    started = time.monotonic()
    sources = retrieve(retrieval_queries(conversation, question))
    messages = build_messages(conversation, question, sources)
    Message.objects.create(conversation=conversation, role=Message.Role.USER, content=question)
    yield {"type": "sources", "sources": [{"number": s.number, "title": s.title, "url": s.url} for s in sources]}

    runner = ToolRunner(conversation)
    usage, model, parts, trace = Usage(), "", [], []
    reply, nudged = None, False
    try:
        for round_no in range(MAX_TOOL_ROUNDS):
            if time.monotonic() - started > ANSWER_DEADLINE_S:
                parts.append("\n\nSorry, this is taking too long. Please try again or call us at (555) 014-7788.")
                yield {"type": "delta", "text": parts[-1]}
                break
            done, round_has_text, round_start = None, False, len(parts)
            for event in get_llm().stream(messages, tools=TOOLS):
                if isinstance(event, Delta):
                    text = event.text
                    if parts and not round_has_text and not parts[-1][-1:].isspace():
                        text = "\n\n" + text.lstrip()  # keep text from separate tool rounds apart
                    round_has_text = True
                    parts.append(text)
                    yield {"type": "delta", "text": text}
                elif isinstance(event, Done):
                    done = event
            usage = Usage(
                usage.prompt_tokens + done.usage.prompt_tokens, usage.completion_tokens + done.usage.completion_tokens
            )
            model = done.model
            if not done.tool_calls:
                round_text = "".join(parts[round_start:])
                if nudged or round_no == MAX_TOOL_ROUNDS - 1 or not is_unbacked_claim(conversation, runner, round_text):
                    break
                nudged = True
                trace.append({"name": "guardrail", "arguments": "{}", "result": {"unbacked_claim": round_text[:300]}})
                messages.append({"role": "assistant", "content": round_text})
                messages.append({"role": "user", "content": UNBACKED_CLAIM_NUDGE})
                continue
            messages.append(
                {
                    "role": "assistant",
                    "content": "".join(parts[round_start:]) or None,
                    "tool_calls": [
                        {"id": c.id, "type": "function", "function": {"name": c.name, "arguments": c.arguments}}
                        for c in done.tool_calls
                    ],
                }
            )
            for call in done.tool_calls:
                yield {"type": "tool", "label": STATUS_LABELS.get(call.name, call.name)}
                proposed_before = len(runner.proposed)
                result = runner.run(call.name, call.arguments)
                trace.append({"name": call.name, "arguments": call.arguments, "result": result})
                messages.append({"role": "tool", "tool_call_id": call.id, "content": json.dumps(result)})
                for action in runner.proposed[proposed_before:]:
                    yield {"type": "action", "id": action.id, "kind": action.kind, "summary": action.summary}
        else:
            parts.append("\n\nSorry, that took too many steps. Could you rephrase or call us at (555) 014-7788?")
            yield {"type": "delta", "text": parts[-1]}
        if not "".join(parts).strip():
            parts.append("Sorry, I couldn't come up with an answer. Could you rephrase that?")
            yield {"type": "delta", "text": parts[-1]}
    finally:
        content = "".join(parts).strip() or "(no answer: the assistant failed before replying)"
        cited = cited_numbers(content)
        reply = Message.objects.create(
            conversation=conversation,
            role=Message.Role.ASSISTANT,
            content=content,
            sources=[asdict(s) | {"text": s.text[:300]} for s in sources if s.number in cited],
            model=model or getattr(get_llm(), "model", ""),
            prompt_tokens=usage.prompt_tokens,
            completion_tokens=usage.completion_tokens,
            cost_usd=usage.cost_usd,
            latency_ms=int((time.monotonic() - started) * 1000),
            tool_calls=trace,
        )
    remaining = settings.ASSISTANT_DAILY_MESSAGE_LIMIT - Message.objects.today_for(conversation.user).count()
    yield {
        "type": "done",
        "message_id": reply.id,
        "cost_usd": float(reply.cost_usd),
        "cited": sorted(cited),
        "remaining": max(remaining, 0),
    }
