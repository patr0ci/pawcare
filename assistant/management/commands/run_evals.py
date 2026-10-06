from django.core.management.base import BaseCommand

from assistant.evals.runner import run_evals


class Command(BaseCommand):
    help = "Score the assistant on the fixed question set (assistant/evals/cases.json) with the configured model."

    def handle(self, *args, **options):
        run = run_evals()
        for r in run.results:
            mark = "PASS" if not r["failures"] else "FAIL"
            self.stdout.write(f"{mark}  {r['question']}" + (f"  → {'; '.join(r['failures'])}" if r["failures"] else ""))
        self.stdout.write(
            self.style.SUCCESS(f"\n{run.passed}/{run.total} passed ({run.score:.0%}) · {run.model} · ${run.cost_usd:.4f}")
        )
