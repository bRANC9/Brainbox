import uuid

from django.db import models

from .scheduling import compute_next_run, describe_schedule, validate_schedule_config


class ScheduleKind(models.TextChoices):
    ONCE = "once", "Once"
    INTERVAL = "interval", "Interval"
    HOURLY = "hourly", "Hourly"
    DAILY = "daily", "Daily"
    WEEKLY = "weekly", "Weekly"
    MONTHLY = "monthly", "Monthly"
    CRON = "cron", "Cron"


class JobRunStatus(models.TextChoices):
    WAITING = "waiting", "Waiting"
    RUNNING = "running", "Running"
    SUCCEEDED = "succeeded", "Succeeded"
    FAILED = "failed", "Failed"
    CANCELLED = "cancelled", "Cancelled"


class JobTrigger(models.TextChoices):
    SCHEDULE = "schedule", "Schedule"
    MANUAL = "manual", "Manual"
    SYSTEM = "system", "System"
    RETRY = "retry", "Retry"


class Job(models.Model):
    """A scheduled (or manual-only) unit of background work."""

    name = models.CharField(max_length=200, unique=True)
    task_key = models.CharField(max_length=100, db_index=True)
    schedule_kind = models.CharField(
        max_length=16, choices=ScheduleKind.choices, default=ScheduleKind.INTERVAL
    )
    schedule_config = models.JSONField(default=dict, blank=True)
    enabled = models.BooleanField(default=True)
    timeout_sec = models.IntegerField(null=True, blank=True)
    max_retries = models.IntegerField(default=0)
    next_run_at = models.DateTimeField(null=True, blank=True, db_index=True)
    last_run_at = models.DateTimeField(null=True, blank=True)
    exhausted = models.BooleanField(
        default=False, help_text="Set for one-shot ('once') jobs after they have fired."
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "jobs_job"
        ordering = ["name"]
        indexes = [models.Index(fields=["enabled", "next_run_at"])]

    def __str__(self) -> str:
        return self.name

    def save(self, *args, **kwargs):
        # First save: prime next_run_at so a fresh deployment starts working.
        # For one-shot jobs an 'at' in the past is intentionally kept (so the
        # scheduler fires it on the next tick and then marks it exhausted).
        if self._state.adding and self.enabled and self.next_run_at is None:
            computed = self.compute_next_run()
            if self.schedule_kind == ScheduleKind.ONCE:
                from .scheduling import parse_iso

                self.next_run_at = parse_iso(self.schedule_config.get("at"))
            else:
                self.next_run_at = computed
        super().save(*args, **kwargs)

    @property
    def config_errors(self) -> list[str]:
        return validate_schedule_config(self.schedule_kind, self.schedule_config)

    @property
    def schedule_description(self) -> str:
        return describe_schedule(self.schedule_kind, self.schedule_config)

    def compute_next_run(self, after=None):
        return compute_next_run(self.schedule_kind, self.schedule_config, after)


class JobRun(models.Model):
    """One execution of a job. The row *is* the queue (no broker needed)."""

    run_id = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    job = models.ForeignKey(Job, on_delete=models.CASCADE, related_name="runs")
    task_key = models.CharField(max_length=100, db_index=True)
    status = models.CharField(
        max_length=16, choices=JobRunStatus.choices, default=JobRunStatus.WAITING, db_index=True
    )
    trigger = models.CharField(
        max_length=16, choices=JobTrigger.choices, default=JobTrigger.SYSTEM
    )
    attempt = models.IntegerField(default=1)
    payload = models.JSONField(default=dict, blank=True)
    dedupe_key = models.CharField(max_length=200, blank=True, db_index=True)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    started_at = models.DateTimeField(null=True, blank=True)
    ended_at = models.DateTimeField(null=True, blank=True)
    duration_ms = models.IntegerField(null=True, blank=True)
    worker = models.CharField(max_length=100, blank=True)
    error = models.TextField(blank=True)
    detail = models.JSONField(default=dict, blank=True)

    class Meta:
        db_table = "jobs_job_run"
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["status", "created_at"]),
            models.Index(fields=["task_key", "status"]),
        ]

    def __str__(self) -> str:
        return f"{self.task_key}#{self.run_id} ({self.status})"

    @property
    def duration_seconds(self) -> float | None:
        if not self.started_at or not self.ended_at:
            return None
        return (self.ended_at - self.started_at).total_seconds()


class SchedulerState(models.Model):
    """Single-row table acting as a cross-process scheduler lock.

    The ai-handler scheduler uses a container-local lock file and documents that
    it must be replaced by a database lock when scaling out. Here the lease
    lives in Postgres, so several replicas/containers can compete safely.
    """

    LOCK_NAME = "scheduler"

    name = models.CharField(max_length=50, unique=True, default=LOCK_NAME)
    owner = models.CharField(max_length=100, blank=True)
    locked_until = models.DateTimeField(null=True, blank=True)
    acquired_at = models.DateTimeField(null=True, blank=True)
    heartbeat_at = models.DateTimeField(null=True, blank=True)
    boot_id = models.CharField(max_length=64, blank=True)

    class Meta:
        db_table = "jobs_scheduler_state"

    def __str__(self) -> str:
        return f"{self.name} owner={self.owner or '-'} until={self.locked_until}"


# Seeded on migrate; mirrors the ai-handler default set, Brainbox-adapted.
DEFAULT_JOBS: list[dict] = [
    {
        "name": "Embedding backfill",
        "task_key": "embedding_backfill",
        "schedule_kind": ScheduleKind.INTERVAL,
        "schedule_config": {"every_minutes": 60},
        "enabled": True,
        "timeout_sec": 1800,
        "max_retries": 1,
    },
    {
        "name": "Git sync (all repositories)",
        "task_key": "git_sync_all",
        "schedule_kind": ScheduleKind.INTERVAL,
        "schedule_config": {"every_minutes": 15},
        "enabled": True,
        "timeout_sec": 1800,
        "max_retries": 1,
    },
    {
        "name": "Stale run recovery",
        "task_key": "recover_stuck_runs",
        "schedule_kind": ScheduleKind.INTERVAL,
        "schedule_config": {"every_minutes": 5},
        "enabled": True,
        "timeout_sec": 300,
        "max_retries": 0,
    },
    {
        "name": "Job history retention",
        "task_key": "prune_job_history",
        "schedule_kind": ScheduleKind.WEEKLY,
        "schedule_config": {"weekdays": [7], "hours": [4], "minute": 0},
        "enabled": True,
        "timeout_sec": 600,
        "max_retries": 1,
    },
    {
        "name": "Audit log retention",
        "task_key": "prune_audit_log",
        "schedule_kind": ScheduleKind.WEEKLY,
        "schedule_config": {"weekdays": [7], "hours": [3], "minute": 30},
        "enabled": True,
        "timeout_sec": 900,
        "max_retries": 1,
    },
]
