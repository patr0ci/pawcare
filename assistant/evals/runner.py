"""Run the fixed eval sets through the real answer pipeline and score them.

Two suites:
- rag (cases.json): one question each, scored on required facts, the article cited, and refusals.
- agent (agent_cases.json): short conversations with a throwaway client account, scored on the proposal the model
  actually created (kind, pet, service, day, time of day) rather than on its wording.

Checks are deliberately simple and transparent, so a failing case is easy to read.
Each case runs inside a transaction that is rolled back, so eval traffic leaves no rows behind.
"""

import hashlib
import json
import os
import re
import subprocess
from datetime import timedelta
from decimal import Decimal
from pathlib import Path

from django.contrib.auth import get_user_model
from django.db import transaction
from django.utils import timezone

from assistant import chat
from assistant.chat import CLAIMS_ACTION, answer
from assistant.llm import get_llm
from assistant.models import Conversation, EvalRun, Message
from assistant.tools import TOOLS, parse_time
from clinic import services
from clinic.demo import create_demo_tutor
from clinic.models import Appointment, Pet, Service

HERE = Path(__file__).parent
CASES = HERE / "cases.json"
AGENT_CASES = HERE / "agent_cases.json"
RESULTS_DIR = HERE / "results"

CITATION = re.compile(r"\[\d+(?:\s*,\s*\d+)*\]")
# Phrases that say "I can't answer that". Suggesting a call to the clinic doesn't count: the prompt asks for it
# in every fallback, so a made-up answer that ends with the phone number would otherwise pass as a refusal.
REFUSAL = re.compile(
    r"don't know|do not know|not sure|don't have|do not have|not (?:able|available|listed|offered|covered)"
    r"|can't|cannot|only help|don't offer|do not offer|not among|aren't (?:among|part|something)"
    r"|isn't (?:among|part|something|listed|offered)",
    re.I,
)


class _Rollback(Exception):
    pass


def score_case(case: dict, text: str, cited_slugs: list[str]) -> list[str]:
    """Returns the list of failed checks (empty = pass)."""
    failures = []
    plain = CITATION.sub("", text)  # so a citation marker like "[1]" can't satisfy the fact "1"
    for needle in case.get("must_include", []):
        if needle.lower() not in plain.lower():
            failures.append(f"missing “{needle}”")
    for pattern in case.get("must_match", []):
        if not re.search(pattern, plain, re.I):
            failures.append(f"no match for /{pattern}/")
    for needle in case.get("must_not_include", []):
        if needle.lower() in plain.lower():
            failures.append(f"should not say “{needle}”")
    for pattern in case.get("must_not_match", []):
        if re.search(pattern, plain, re.I):
            failures.append(f"should not match /{pattern}/")
    if case.get("must_cite"):
        # One slug, or a list when the fact legitimately lives in more than one article.
        accepted = case["must_cite"] if isinstance(case["must_cite"], list) else [case["must_cite"]]
        if not set(accepted) & set(cited_slugs):
            failures.append(f"did not cite {' or '.join(accepted)}")
    if case.get("must_refuse") and not REFUSAL.search(plain):
        failures.append("should have said it doesn't know")
    return failures


def run_case(case: dict, user) -> dict:
    result = {"suite": "rag", "question": case["question"], "answer": "", "cited": [], "cost_usd": 0.0,
              "latency_ms": 0, "tools": []}
    try:
        with transaction.atomic():
            conversation = Conversation.objects.create(user=user)
            events = list(answer(conversation, case["question"]))
            reply = conversation.messages.last()
            text = reply.content
            cited = [s["url"].strip("/").split("/")[-1] for s in reply.sources]
            result |= {
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
    except Exception as exc:  # one provider hiccup shouldn't lose the whole run
        result["failures"] = [f"error: {type(exc).__name__}: {exc}"[:300]]
    return result


# --- agent suite ---------------------------------------------------------------------------------------------


def setup_agent_case(case: dict, tutor) -> Appointment | None:
    """Optional fixtures: pet fields the client typed (e.g. an injection attempt) and an existing appointment."""
    for name, fields in case.get("pets", {}).items():
        tutor.pets.filter(name=name).update(**fields)
    spec = case.get("appointment")
    if not spec:
        return None
    pet = tutor.pets.get(name=spec["pet"])
    service = Service.objects.get(name=spec["service"])
    starts_at = (timezone.now() + timedelta(hours=spec["hours_ahead"])).replace(minute=0, second=0, microsecond=0)
    return Appointment.objects.create(pet=pet, vet=services.vets_for(pet, service)[0], service=service, starts_at=starts_at)


def next_dates(weekday: str, count: int = 2) -> list:
    """The next `count` dates that fall on `weekday` after today. "Next Tuesday" is ambiguous in English,
    so both the coming one and the one after are accepted."""
    today = timezone.localdate()
    days = [today + timedelta(days=i) for i in range(1, 15)]
    return [d for d in days if d.strftime("%A") == weekday][:count]


def score_agent_case(case: dict, text: str, actions: list, appointment, trace: list[dict]) -> list[str]:
    expect = case["expect"]
    failures = score_case(case.get("checks", {}), text, [])
    claims = bool(CLAIMS_ACTION.search(text))
    action = actions[-1] if actions else None  # the latest proposal is the one the client would confirm
    if expect["action"] == "none":
        if action:
            failures.append(f"proposed a {action.kind} but shouldn't have")
        elif claims:
            failures.append("claims an action that was never proposed")
    elif action is None:
        failures.append(f"no {expect['action']} proposal" + (" (but the reply claims one)" if claims else ""))
    else:
        p = action.payload
        if action.kind != expect["action"]:
            failures.append(f"proposed a {action.kind}, expected a {expect['action']}")
        if "appointment_id" in p and appointment and p["appointment_id"] != appointment.id:
            failures.append("acted on the wrong appointment")
        if "pet" in expect and "pet_id" in p and Pet.objects.get(id=p["pet_id"]).name != expect["pet"]:
            failures.append(f"wrong pet (expected {expect['pet']})")
        if "service" in expect and "service_id" in p and Service.objects.get(id=p["service_id"]).name != expect["service"]:
            failures.append(f"wrong service (expected {expect['service']})")
        if "starts_at" in p:
            start = timezone.localtime(parse_time(p["starts_at"]))
            if "weekday" in expect and start.date() not in next_dates(expect["weekday"]):
                failures.append(f"booked {start:%a %b %-d}, expected the next {expect['weekday']}")
            if expect.get("part_of_day") == "morning" and start.hour >= 12:
                failures.append(f"{start:%-I:%M %p} is not in the morning")
            if expect.get("part_of_day") == "afternoon" and start.hour < 12:
                failures.append(f"{start:%-I:%M %p} is not in the afternoon")
    tool_errors = sum(1 for c in trace if isinstance(c.get("result"), dict) and "error" in c["result"])
    if tool_errors > 1:  # the prompt allows one retry after an error
        failures.append(f"{tool_errors} tool errors")
    return failures


def run_agent_case(case: dict) -> dict:
    result = {"suite": "agent", "question": " → ".join(case["turns"]), "answer": "", "cited": [], "cost_usd": 0.0,
              "latency_ms": 0, "tools": [], "guardrail": False}
    try:
        with transaction.atomic():
            tutor = create_demo_tutor()
            appointment = setup_agent_case(case, tutor)
            conversation = Conversation.objects.create(user=tutor.user)
            for turn in case["turns"]:
                list(answer(conversation, turn))
            replies = list(conversation.messages.filter(role=Message.Role.ASSISTANT))
            trace = [call for r in replies for call in r.tool_calls]
            actions = list(conversation.actions.order_by("created_at"))
            result |= {
                "answer": replies[-1].content,
                "failures": score_agent_case(case, replies[-1].content, actions, appointment, trace),
                "cost_usd": float(sum(r.cost_usd for r in replies)),
                "latency_ms": sum(r.latency_ms for r in replies),
                "tools": [call["name"] for call in trace],
                "guardrail": any(call["name"] == "guardrail" for call in trace),
            }
            raise _Rollback
    except _Rollback:
        pass
    except Exception as exc:
        result["failures"] = [f"error: {type(exc).__name__}: {exc}"[:300]]
    return result


# --- running and recording ---------------------------------------------------------------------------------------


def repeated(run_once, case, times: int) -> dict:
    """Run a case `times` times. It passes only if every run passes; a failing run is the one shown."""
    attempts = [run_once(case) for _ in range(times)]
    failing = [a for a in attempts if a["failures"]]
    return (failing[0] if failing else attempts[-1]) | {
        "runs": times,
        "passed_runs": times - len(failing),
        "cost_usd": sum(a["cost_usd"] for a in attempts),
        "guardrail_runs": sum(1 for a in attempts if a.get("guardrail")),
    }


def sha(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()[:12]


def git_sha() -> str:
    if os.getenv("GIT_SHA"):  # set at image build time; the image has no .git
        return os.getenv("GIT_SHA")
    try:
        out = subprocess.run(["git", "rev-parse", "--short", "HEAD"], capture_output=True, text=True, timeout=5)
        return out.stdout.strip()
    except OSError:
        return ""


def run_evals(cases_path: Path | None = CASES, agent_cases_path: Path | None = AGENT_CASES, repeat: int = 1) -> EvalRun:
    results, cases_text = [], ""
    if cases_path:
        cases_text += cases_path.read_text()
        user, _ = get_user_model().objects.get_or_create(username="eval-bot")
        results += [repeated(lambda c: run_case(c, user), case, repeat) for case in json.loads(cases_path.read_text())]
    if agent_cases_path:
        cases_text += agent_cases_path.read_text()
        results += [repeated(run_agent_case, case, repeat) for case in json.loads(agent_cases_path.read_text())]
    suites = {}
    for r in results:
        s = suites.setdefault(r["suite"], {"passed": 0, "total": 0, "guardrail_runs": 0})
        s["total"] += 1
        s["passed"] += not r["failures"]
        s["guardrail_runs"] += r["guardrail_runs"]
    meta = {
        "git_sha": git_sha(),
        "prompt_sha": sha(chat.SYSTEM_PROMPT + chat.UNBACKED_CLAIM_NUDGE + json.dumps(TOOLS)),
        "cases_sha": sha(cases_text),
        "repeat": repeat,
        "suites": suites,
    }
    return EvalRun.objects.create(
        model=getattr(get_llm(), "model", "unknown"),
        total=len(results),
        passed=sum(1 for r in results if not r["failures"]),
        cost_usd=Decimal(str(round(sum(r["cost_usd"] for r in results), 6))),
        results=results,
        meta=meta,
    )


def save_run(run: EvalRun, directory: Path = RESULTS_DIR) -> Path:
    """Write the run next to the cases, so a score in the README can be checked without the live demo."""
    directory.mkdir(parents=True, exist_ok=True)
    model = re.sub(r"[^a-z0-9.]+", "-", run.model.lower()).strip("-")
    path = directory / f"{timezone.localtime(run.created_at):%Y-%m-%d}-{model}.json"
    data = {
        "model": run.model,
        "created_at": run.created_at.isoformat(),
        "passed": run.passed,
        "total": run.total,
        "cost_usd": float(run.cost_usd),
        "meta": run.meta,
        "results": run.results,
    }
    path.write_text(json.dumps(data, indent=1, ensure_ascii=False) + "\n")
    return path
