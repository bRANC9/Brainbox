"""DB-driven scheduler engine (ported from the ai-handler scheduler design).

Deliberate differences vs. the original (see the analysis in README):
- **The lock is a database lease** (SchedulerState), not a container-local file,
  so multiple replicas cannot double-fire a schedule.
- **The run row is the queue.** Workers poll and atomically claim rows, so a lost
  in-memory queue hand-off (the original's "orphan waiting runs" problem) cannot
  happen, and scheduler/worker can live in separate containers.
- Optional IANA timezone per schedule (``schedule_config["timezone"]``).
- Timeout enforcement uses SIGALRM where available, with a documented
  best-effort thread fallback.

Semantics kept identical: skip-if-busy (no run pile-up), missed occurrences are
skipped, atomic ``waiting -> running`` claim, retry with attempt+1 up to
``max_retries``, cancel, and age-aware recovery of stale running/waiting runs.
"""

from __future__ import annotations

import logging
import os
import signal
import socket
from contextlib import contextmanager
from datetime import timedelta

from django.conf import settings
from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from .models import DEFAULT_JOBS, Job, JobRun, JobRunStatus, JobTrigger, SchedulerState
from .registry import JobContext, get_job, list_jobs

logger = logging.getLogger("brainbox.jobs")

BOOT_ID = f"{socket.gethostname()}:{os.getpid()}"


# ---------------------------------------------------------------------------
# Locking
# ---------------------------------------------------------------------------
def _lock_ttl() -> timedelta:
    return timedelta(seconds=max(10, settings.BRAINBOX_JOB_LOCK_TTL_SEC))


def acquire_lock(owner: str | None = None) -> bool:
    """Try to become the single scheduler owner. Returns True when acquired."""
    owner = owner or BOOT_ID
    now = timezone.now()
    until = now + _lock_ttl()
    with transaction.atomic():
        state, _created = SchedulerState.objects.get_or_create(name=SchedulerState.LOCK_NAME)
        claimed = SchedulerState.objects.filter(
            pk=state.pk
        ).filter(Q(locked_until__isnull=True) | Q(locked_until__lt=now)).update(
            owner=owner,
            locked_until=until,
            acquired_at=now,
            heartbeat_at=now,
            boot_id=BOOT_ID,
        )
    return claimed > 0


def release_lock() -> None:
    SchedulerState.objects.filter(name=SchedulerState.LOCK_NAME, boot_id=BOOT_ID).update(
        locked_until=None, heartbeat_at=timezone.now()
    )


def heartbeat() -> None:
    SchedulerState.objects.filter(name=SchedulerState.LOCK_NAME, boot_id=BOOT_ID).update(
        locked_until=timezone.now() + _lock_ttl(), heartbeat_at=timezone.now()
    )


def owns_lock() -> bool:
    return SchedulerState.objects.filter(
        name=SchedulerState.LOCK_NAME, owner=BOOT_ID, locked_until__gt=timezone.now()
    ).exists()


@contextmanager
def scheduler_lock():
    """Yield True when this process owns the lease for the block."""
    acquired = acquire_lock()
    try:
        yield acquired
    finally:
        if acquired:
            release_lock()


def status() -> dict:
    state = SchedulerState.objects.filter(name=SchedulerState.LOCK_NAME).first()
    counts = {
        row["status"]: row["count"]
        for row in JobRun.objects.values("status").annotate(count=_count_id())
    }
    return {
        "enabled": settings.BRAINBOX_SCHEDULER_ENABLED,
        "owns_lock": bool(state and state.boot_id == BOOT_ID and state.locked_until and state.locked_until > timezone.now()),
        "lock_owner": state.owner if state else "",
        "locked_until": state.locked_until if state else None,
        "running_jobs": counts.get(JobRunStatus.RUNNING, 0),
        "waiting_jobs": counts.get(JobRunStatus.WAITING, 0),
        "registered_tasks": len(list_jobs()),
    }


def _count_id():
    from django.db.models import Count

    return Count("id")


# ---------------------------------------------------------------------------
# Enqueue / tick
# ---------------------------------------------------------------------------
def _dedupe_key(task_key: str, payload: dict | None) -> str:
    spec = get_job(task_key)
    field = spec.dedupe_key if spec else None
    if not field or not payload:
        return ""
    value = payload.get(field)
    return f"{task_key}:{field}:{value}" if value not in (None, "") else ""


def enqueue_run(task_key: str, *, trigger: str = JobTrigger.SYSTEM, payload: dict | None = None) -> JobRun | None:
    """Create a waiting run row. Returns None when deduped against active work."""
    if get_job(task_key) is None:
        raise KeyError(f"No job registered under task key '{task_key}'")

    payload = payload or {}
    dedupe = _dedupe_key(task_key, payload)
    if dedupe and JobRun.objects.filter(
        dedupe_key=dedupe, status__in=[JobRunStatus.WAITING, JobRunStatus.RUNNING]
    ).exists():
        logger.info("Skipping %s enqueue: active run for %s", task_key, dedupe)
        return None

    job = Job.objects.filter(task_key=task_key).first()
    if job is None:
        # Shell job (disabled, invalid interval) so the run shows up in the UI.
        job = Job.objects.create(
            name=task_key,
            task_key=task_key,
            schedule_kind="interval",
            schedule_config={"every_minutes": 0},
            enabled=False,
        )
    return JobRun.objects.create(
        job=job,
        task_key=task_key,
        status=JobRunStatus.WAITING,
        trigger=trigger,
        attempt=1,
        payload=payload,
        dedupe_key=dedupe,
    )


@transaction.atomic
def tick(now=None) -> int:
    """Create run rows for due jobs. Returns the number of occurrences enqueued."""
    if not settings.BRAINBOX_SCHEDULER_ENABLED:
        return 0
    now = now or timezone.now()

    busy_job_ids = set(
        JobRun.objects.filter(status__in=[JobRunStatus.WAITING, JobRunStatus.RUNNING]).values_list(
            "job_id", flat=True
        )
    )

    enqueued = 0
    for job in Job.objects.filter(enabled=True):
        errors = job.config_errors
        if errors:
            logger.warning("Job %s has invalid schedule, skipping: %s", job.name, "; ".join(errors))
            continue

        if job.next_run_at is None:
            computed = job.compute_next_run(now)
            if computed:
                Job.objects.filter(pk=job.pk).update(next_run_at=computed)
            continue

        next_run = job.next_run_at
        if next_run.tzinfo is None:
            next_run = next_run.replace(tzinfo=timezone.utc)
        if next_run > now:
            continue

        if job.pk in busy_job_ids:
            # Keep the schedule alive but skip this occurrence.
            Job.objects.filter(pk=job.pk).update(next_run_at=job.compute_next_run(now))
            continue

        run = JobRun.objects.create(
            job=job,
            task_key=job.task_key,
            status=JobRunStatus.WAITING,
            trigger=JobTrigger.SCHEDULE,
            attempt=1,
            dedupe_key=_dedupe_key(job.task_key, {}),
        )
        Job.objects.filter(pk=job.pk).update(
            last_run_at=now, next_run_at=job.compute_next_run(now)
        )
        enqueued += 1
        logger.info("Scheduled %s as run %s", job.name, run.run_id)

    return enqueued


def recover_stuck_runs(grace_sec: int | None = None) -> int:
    """Age-aware reclaim of waiting/running rows left behind by a dead worker."""
    grace = grace_sec or max(600, settings.BRAINBOX_JOB_POLL_SEC * 4)
    cutoff = timezone.now() - timedelta(seconds=grace)
    waiting = JobRun.objects.filter(
        status=JobRunStatus.WAITING, created_at__lt=cutoff
    ).update(status=JobRunStatus.FAILED, error="orphaned waiting run recovered", ended_at=timezone.now())
    running = JobRun.objects.filter(status=JobRunStatus.RUNNING, started_at__lt=cutoff).update(
        status=JobRunStatus.FAILED, error="orphaned running run recovered", ended_at=timezone.now()
    )
    total = waiting + running
    if total:
        logger.warning("Recovered %s orphaned job run(s)", total)
    return total


# ---------------------------------------------------------------------------
# Execution
# ---------------------------------------------------------------------------
def claim_next_run(worker: str) -> JobRun | None:
    """Atomically claim the oldest waiting run (waiting -> running)."""
    with transaction.atomic():
        candidate = (
            JobRun.objects.select_for_update(skip_locked=True)
            .filter(status=JobRunStatus.WAITING)
            .order_by("created_at")
            .first()
        )
        if candidate is None:
            return None
        claimed = JobRun.objects.filter(pk=candidate.pk, status=JobRunStatus.WAITING).update(
            status=JobRunStatus.RUNNING,
            started_at=timezone.now(),
            worker=worker[:100],
        )
        if claimed == 0:
            return None  # another worker won the race
    return JobRun.objects.get(pk=candidate.pk)


@contextmanager
def _timeout(seconds: int | None):
    """Best-effort hard timeout: SIGALRM where available, else no-op."""
    if not seconds:
        yield
        return
    try:
        def _handler(signum, frame):  # noqa: ARG001
            raise TimeoutError(f"timeout after {seconds}s")

        previous = signal.signal(signal.SIGALRM, _handler)
        signal.alarm(int(seconds))
        try:
            yield
        finally:
            signal.alarm(0)
            signal.signal(signal.SIGALRM, previous)
    except (ValueError, AttributeError):
        # Not the main thread (or no SIGALRM): run without a hard timeout.
        yield


def execute_run(run: JobRun) -> None:
    """Execute a claimed run in-process: handler + timeout + retry bookkeeping."""
    spec = get_job(run.task_key)
    job = run.job
    timeout_sec = job.timeout_sec if job else None
    started = timezone.now()
    try:
        if spec is None:
            raise LookupError(f"Task '{run.task_key}' is not registered")
        context = JobContext(
            run=run,
            job=job,
            job_id=job.pk if job else None,
            job_name=job.name if job else None,
            task_key=run.task_key,
            run_id=str(run.run_id),
            attempt=run.attempt,
            config=dict(job.schedule_config) if job else {},
            payload=dict(run.payload or {}),
        )
        with _timeout(timeout_sec):
            detail = spec.handler(context) or {}
        _finish(run, JobRunStatus.SUCCEEDED, started=started, detail=detail)
    except TimeoutError as exc:
        _finish(run, JobRunStatus.FAILED, started=started, error=str(exc))
        _maybe_retry(run)
    except Exception as exc:  # noqa: BLE001
        logger.exception("Job run %s failed", run.run_id)
        _finish(run, JobRunStatus.FAILED, started=started, error=f"{type(exc).__name__}: {exc}")
        _maybe_retry(run)


def _finish(run: JobRun, status: str, *, started, error: str | None = None, detail: dict | None = None) -> None:
    ended = timezone.now()
    fresh = JobRun.objects.filter(pk=run.pk).first()
    if fresh is None or fresh.status == JobRunStatus.CANCELLED:
        return
    if status == JobRunStatus.SUCCEEDED:
        fresh.status = status
    else:
        fresh.status = status
    fresh.ended_at = ended
    fresh.duration_ms = int((ended - started).total_seconds() * 1000)
    if detail is not None:
        fresh.detail = detail
    if error is not None:
        fresh.error = error[:4000]
    fresh.save(
        update_fields=["status", "ended_at", "duration_ms", "detail", "error"]
    )


def _maybe_retry(run: JobRun) -> None:
    job = run.job
    max_retries = job.max_retries if job else 0
    if max_retries <= 0 or run.attempt >= max_retries + 1:
        return
    JobRun.objects.create(
        job=job,
        task_key=run.task_key,
        status=JobRunStatus.WAITING,
        trigger=JobTrigger.RETRY,
        attempt=run.attempt + 1,
        payload=dict(run.payload or {}),
        dedupe_key=run.dedupe_key,
    )
    logger.info("Re-enqueued %s as retry attempt %s", run.run_id, run.attempt + 1)


def cancel_run(run: JobRun) -> bool:
    """Mark a waiting/running run cancelled (best effort for running tasks)."""
    if run.status not in {JobRunStatus.WAITING, JobRunStatus.RUNNING}:
        return False
    JobRun.objects.filter(pk=run.pk).update(
        status=JobRunStatus.CANCELLED, ended_at=timezone.now(), error="cancelled"
    )
    return True


def run_pending_once(worker: str = BOOT_ID) -> JobRun | None:
    run = claim_next_run(worker)
    if run is None:
        return None
    execute_run(run)
    return run


# ---------------------------------------------------------------------------
# Seeding
# ---------------------------------------------------------------------------
def seed_default_jobs() -> int:
    created = 0
    for job_def in DEFAULT_JOBS:
        _job, was_created = Job.objects.get_or_create(name=job_def["name"], defaults=job_def)
        created += int(was_created)
    return created
