"""One-off job operations: inspect, trigger, tick, recover, prune."""

import json

from django.conf import settings
from django.core.management.base import BaseCommand
from django.utils import timezone

from apps.jobs import engine, tasks_brainbox  # noqa: F401  (registers handlers)
from apps.jobs.models import JobRun, JobRunStatus, JobTrigger
from apps.jobs.registry import list_jobs


class Command(BaseCommand):
    help = "Inspect and trigger background jobs."

    def add_arguments(self, parser):
        parser.add_argument("--status", action="store_true", help="Scheduler/queue status.")
        parser.add_argument("--list-tasks", action="store_true", help="List registered task keys.")
        parser.add_argument("--runs", action="store_true", help="List recent run rows.")
        parser.add_argument("--run", default=None, help="Enqueue a task key.")
        parser.add_argument("--run-now", action="store_true", help="Execute --run synchronously.")
        parser.add_argument("--payload", default=None, help="JSON payload for --run.")
        parser.add_argument("--tick", action="store_true", help="Run a single scheduler tick.")
        parser.add_argument("--recover", action="store_true", help="Reclaim stale runs.")
        parser.add_argument("--prune", action="store_true", help="Prune finished run history.")

    def handle(self, *args, **options):
        if options["list_tasks"]:
            for spec in list_jobs():
                self.stdout.write(f"{spec.key}\t{spec.description}")
            return

        if options["status"]:
            for key, value in engine.status().items():
                self.stdout.write(f"{key}: {value}")
            return

        if options["runs"]:
            for run in JobRun.objects.select_related("job")[:20]:
                self.stdout.write(
                    f"{run.created_at:%Y-%m-%d %H:%M} {run.task_key} {run.status} "
                    f"attempt={run.attempt} {run.duration_ms or 0}ms {run.error[:80]}"
                )
            return

        if options["tick"]:
            with engine.scheduler_lock() as owns:
                self.stdout.write(f"tick enqueued={engine.tick()} owns_lock={owns}")
            return

        if options["recover"]:
            self.stdout.write(f"recovered={engine.recover_stuck_runs()}")
            return

        if options["prune"]:
            self.stdout.write(f"deleted={self._prune(settings.BRAINBOX_JOB_HISTORY_DAYS)}")
            return

        if options["run"]:
            payload = json.loads(options["payload"]) if options["payload"] else {}
            run = engine.enqueue_run(options["run"], trigger=JobTrigger.MANUAL, payload=payload)
            if run is None:
                self.stdout.write("skipped: an active run already covers this dedupe key")
                return
            self.stdout.write(f"enqueued run {run.run_id}")
            if options["run_now"]:
                engine.run_pending_once()
                run.refresh_from_db()
                self.stdout.write(
                    f"status={run.status} detail={run.detail} error={run.error[:200]}"
                )
            return

        self.stdout.write("nothing requested; try --status / --list-tasks")

    @staticmethod
    def _prune(days: int) -> int:
        from datetime import timedelta

        cutoff = timezone.now() - timedelta(days=days)
        deleted, _ = JobRun.objects.filter(
            created_at__lt=cutoff,
            status__in=[
                JobRunStatus.SUCCEEDED,
                JobRunStatus.FAILED,
                JobRunStatus.CANCELLED,
            ],
        ).delete()
        return deleted
