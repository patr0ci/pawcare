"""Run the fixed question set through the real answer pipeline and score it.

Checks are deliberately simple and transparent (substring + cited article), so a failing case is easy to read.
Each case runs in a throwaway conversation and the generated rows are rolled back.
"""

import json
import re
from decimal import Decimal
from pathlib import Path

from django.contrib.auth import get_user_model
from django.db import transaction

from assistant.chat import answer
from assistant.llm import get_llm
from assistant.models import Conversation, EvalRun

CASES = Path(__file__).parent / "cases.json"
REFUSAL = re.compile(r"don't know|do not know|not sure|don't have|do not have|not (?:able|available)|can't|cannot|call (?:us|the clinic)|(?:555\) 014-7788)", re.I)


class _Rollback(Exception):
    pass


def score_case(case: dict, text: str, cited_slugs: list[str]) -> list[str]:
    """Returns the list of failed checks (empty = pass)."""
    failures = []
    for needle in case.get("must_include", []):
        if needle.lower() not in text.lower():
            failures.append(f"missing “{needle}”")
    for needle in case.get("must_not_include", []):
        if needle.lower() in text.lower():
            failures.append(f"should not say “{needle}”")
    if case.get("must_cite"):
        # One slug, or a list when the fact legitimately lives in more than one article.
        accepted = case["must_cite"] if isinstance(case["must_cite"], list) else [case["must_cite"]]
        if not set(accepted) & set(cited_slugs):
            failures.append(f"did not cite {' or '.join(accepted)}")
    if case.get("must_refuse") and not REFUSAL.search(text):
        failures.append("should have said it doesn't know")
    return failures


def run_case(case: dict, user) -> dict:
    result = {}
    try:
        with transaction.atomic():
            conversation = Conversation.objects.create(user=user)
            events = list(answer(conversation, case["question"]))
            reply = conversation.messages.last()
            text = reply.content
            cited = [s["url"].strip("/").split("/")[-1] for s in reply.sources]
            result = {
                "question": case["question"],
                "answer": text,
                "cited": cited,
                "failures": score_case(case, text, cited),
                "cost_usd": float(reply.cost_usd),
                "latency_ms": reply.latency_ms,
                "tools": [e["label"] for e in events if e["type"] == "tool"],
            }
            raise _Rollback
    except _Rollback:
        pass
    return result


def run_evals(cases_path: Path = CASES) -> EvalRun:
    cases = json.loads(cases_path.read_text())
    user, _ = get_user_model().objects.get_or_create(username="eval-bot")
    results = [run_case(case, user) for case in cases]
    return EvalRun.objects.create(
        model=getattr(get_llm(), "model", "unknown"),
        total=len(results),
        passed=sum(1 for r in results if not r["failures"]),
        cost_usd=Decimal(str(sum(r["cost_usd"] for r in results))),
        results=results,
    )
