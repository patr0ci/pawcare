from django.core.management.base import BaseCommand

from assistant.evals.runner import AGENT_CASES, CASES, run_evals, save_run


class Command(BaseCommand):
    help = (
        "Score the assistant with the configured model: help-center answers (assistant/evals/cases.json) and "
        "booking conversations (assistant/evals/agent_cases.json)."
    )

    def add_arguments(self, parser):
        parser.add_argument("--suite", choices=["all", "rag", "agent"], default="all")
        parser.add_argument("--repeat", type=int, default=1, help="Run each case N times; it passes only if all pass.")
        parser.add_argument("--save", action="store_true", help="Also write the run to assistant/evals/results/.")

    def handle(self, *args, **options):
        suite = options["suite"]
        run = run_evals(
            cases_path=CASES if suite in ("all", "rag") else None,
            agent_cases_path=AGENT_CASES if suite in ("all", "agent") else None,
            repeat=options["repeat"],
        )
        for r in run.results:
            mark = "PASS" if not r["failures"] else "FAIL"
            runs = f" [{r['passed_runs']}/{r['runs']}]" if r["runs"] > 1 else ""
            guard = " (guardrail)" if r.get("guardrail_runs") else ""
            reasons = f"  → {'; '.join(r['failures'])}" if r["failures"] else ""
            self.stdout.write(f"{mark}{runs}{guard}  {r['suite']:5}  {r['question']}{reasons}")
        for name, s in run.meta["suites"].items():
            self.stdout.write(
                f"{name}: {s['passed']}/{s['total']}"
                + (f", guardrail fired in {s['guardrail_runs']} runs" if s["guardrail_runs"] else "")
            )
        self.stdout.write(
            self.style.SUCCESS(
                f"\n{run.passed}/{run.total} passed ({run.score:.0%}) · {run.model} · ${run.cost_usd:.4f}"
            )
        )
        if options["save"]:
            self.stdout.write(f"Saved to {save_run(run)}")
