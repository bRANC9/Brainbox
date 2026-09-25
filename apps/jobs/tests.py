from datetime import timedelta

from django.test import TestCase, override_settings
from django.utils import timezone

from apps.jobs import engine
from apps.jobs.models import Job, JobRun, JobRunStatus, JobTrigger
from apps.jobs.registry import JobContext, register_job
from apps.jobs.scheduling import (
    compute_next_run,
    describe_schedule,
    validate_schedule_config,
)


class ScheduleMathTests(TestCase):
    def test_interval_next_run(self):
        base = timezone.now()
        nxt = compute_next_run("interval", {"every_minutes": 30}, base)
        self.assertEqual(round((nxt - base).total_seconds()), 30 * 60)

    def test_daily_next_run(self):
        base = timezone.now()
        nxt = compute_next_run("daily", {"hours": [3], "minute": 0}, base)
        self.assertIsNotNone(nxt)
        self.assertGreater(nxt, base)

    def test_weekly_and_monthly_never_none(self):
        base = timezone.now()
        self.assertIsNotNone(compute_next_run("weekly", {"weekdays": [7], "hours": [4]}, base))
        self.assertIsNotNone(compute_next_run("monthly", {"days_of_month": [1], "hours": [4]}, base))

    def test_invalid_configs_reported_not_raised(self):
        self.assertTrue(validate_schedule_config("interval", {"every_minutes": 0}))
        self.assertTrue(validate_schedule_config("daily", {"hours": []}))
        self.assertTrue(validate_schedule_config("weekly", {"weekdays": [9], "hours": [2]}))
        self.assertTrue(validate_schedule_config("unknown", {}))
        self.assertEqual(validate_schedule_config("interval", {"every_minutes": 5}), [])

    def test_describe_is_human_readable(self):
        self.assertIn("minute", describe_schedule("interval", {"every_minutes": 30}))
        self.assertIn("daily", describe_schedule("daily", {"hours": [3], "minute": 0}))


class SchedulerEngineTests(TestCase):
    def setUp(self):
        self.job = Job.objects.create(
            name="test job",
            task_key="test_task",
            schedule_kind="interval",
            schedule_config={"every_minutes": 1},
            enabled=True,
            max_retries=1,
        )

    def test_tick_creates_run_for_due_job(self):
        Job.objects.filter(pk=self.job.pk).update(next_run_at=timezone.now() - timedelta(minutes=1))
        enqueued = engine.tick()
        self.assertEqual(enqueued, 1)
        self.assertTrue(JobRun.objects.filter(job=self.job, status=JobRunStatus.WAITING).exists())

    def test_tick_skips_when_job_already_busy(self):
        JobRun.objects.create(job=self.job, task_key="test_task", status=JobRunStatus.RUNNING)
        Job.objects.filter(pk=self.job.pk).update(next_run_at=timezone.now() - timedelta(minutes=1))
        enqueued = engine.tick()
        self.assertEqual(enqueued, 0)  # occurrence skipped, schedule stays alive

    def test_lock_is_exclusive(self):
        self.assertTrue(engine.acquire_lock("worker-a"))
        self.assertFalse(engine.acquire_lock("worker-b"))  # lease held
        engine.release_lock()
        self.assertTrue(engine.acquire_lock("worker-b"))

    def test_execute_run_success_and_retry_on_failure(self):
        calls = {"n": 0}

        @register_job("flaky_test", "test")
        def _flaky(context):
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("boom")
            return {"ok": True}

        # A persistent job with max_retries=1 so the retry path is exercised.
        Job.objects.create(
            name="flaky job",
            task_key="flaky_test",
            schedule_kind="interval",
            schedule_config={"every_minutes": 0},  # invalid: manual runs only
            enabled=False,
            max_retries=1,
        )
        run = engine.enqueue_run("flaky_test", trigger=JobTrigger.MANUAL)
        engine.run_pending_once()
        run.refresh_from_db()
        self.assertEqual(run.status, JobRunStatus.FAILED)
        self.assertIn("boom", run.error)

        # A retry run was enqueued (max_retries=1 -> attempt 2)
        retry = JobRun.objects.filter(task_key="flaky_test", attempt=2).first()
        self.assertIsNotNone(retry)
        engine.run_pending_once()
        retry.refresh_from_db()
        self.assertEqual(retry.status, JobRunStatus.SUCCEEDED)
        self.assertEqual(retry.detail, {"ok": True})

    def test_dedupe_prevents_double_enqueue(self):
        @register_job("dedupe_test", "test", dedupe_key="document_id")
        def _dedupe(context):
            return {}

        first = engine.enqueue_run("dedupe_test", payload={"document_id": 1})
        self.assertIsNotNone(first)
        second = engine.enqueue_run("dedupe_test", payload={"document_id": 1})
        self.assertIsNone(second)  # active run for same dedupe key

    def test_recover_stuck_runs(self):
        old_waiting = JobRun.objects.create(job=self.job, task_key="test_task", status=JobRunStatus.WAITING)
        JobRun.objects.filter(pk=old_waiting.pk).update(
            created_at=timezone.now() - timedelta(hours=2)
        )
        old_running = JobRun.objects.create(job=self.job, task_key="test_task", status=JobRunStatus.RUNNING)
        JobRun.objects.filter(pk=old_running.pk).update(started_at=timezone.now() - timedelta(hours=2))
        recovered = engine.recover_stuck_runs(grace_sec=600)
        self.assertEqual(recovered, 2)
        self.assertEqual(JobRun.objects.get(pk=old_waiting.pk).status, JobRunStatus.FAILED)

    def test_cancel_waiting_run(self):
        @register_job("cancel_test", "test")
        def _noop(context):
            return {}

        run = engine.enqueue_run("cancel_test", trigger=JobTrigger.MANUAL)
        self.assertTrue(engine.cancel_run(run))
        run.refresh_from_db()
        self.assertEqual(run.status, JobRunStatus.CANCELLED)


@override_settings(BRAINBOX_JOB_HISTORY_DAYS=30)
class DefaultJobsSeedTests(TestCase):
    def test_defaults_seeded_by_migration(self):
        from apps.jobs.models import DEFAULT_JOBS

        for job_def in DEFAULT_JOBS:
            self.assertTrue(Job.objects.filter(name=job_def["name"]).exists())


class JobContextTests(TestCase):
    def test_context_fields(self):
        ctx = JobContext(task_key="k", attempt=2, payload={"a": 1})
        self.assertEqual(ctx.task_key, "k")
        self.assertEqual(ctx.attempt, 2)
        self.assertEqual(ctx.payload["a"], 1)
