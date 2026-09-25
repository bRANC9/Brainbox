"""Prometheus domain metrics for Brainbox.

A single collector so the /metrics scrape stays cheap (a handful of COUNT
queries) and works on both PostgreSQL and SQLite.
"""

from __future__ import annotations

import logging

from prometheus_client.core import GaugeMetricFamily
from prometheus_client.registry import Collector

logger = logging.getLogger("brainbox.monitoring")

try:  # pragma: no cover - prometheus_client comes with django-prometheus
    from prometheus_client import REGISTRY
except ImportError:  # pragma: no cover
    REGISTRY = None

_REGISTERED = False


class BrainboxCollector(Collector):
    """Exposes domain-level gauges (content, indexing, git, secrets, jobs)."""

    def describe(self):
        # Registration must not touch the database; metrics are only described
        # at scrape time. Returning an empty descriptor list keeps the names
        # out of the static registry while still exposing them on /metrics.
        return []

    def collect(self):
        yield from self._safe(self._content)
        yield from self._safe(self._indexing)
        yield from self._safe(self._git)
        yield from self._safe(self._secrets)
        yield from self._safe(self._jobs)
        yield from self._safe(self._audit)

    # -- individual metric groups -------------------------------------------
    def _content(self):
        from apps.documents.models import Document
        from apps.embeddings.models import KnowledgeChunk
        from apps.links.models import ResourceLink
        from apps.resources.models import Resource

        documents = GaugeMetricFamily(
            "brainbox_documents", "Documents by status", labels=["status"]
        )
        for row in Document.objects.values("status").annotate(count=_count("resource_id")):
            documents.add_metric([row["status"]], row["count"])
        yield documents

        chunks = GaugeMetricFamily("brainbox_chunks", "Indexed knowledge chunks")
        chunks.add_metric([], KnowledgeChunk.objects.count())
        yield chunks

        links = GaugeMetricFamily("brainbox_resource_links", "Resource links")
        links.add_metric([], ResourceLink.objects.count())
        yield links

        resources = GaugeMetricFamily(
            "brainbox_resources", "Resources by type", labels=["type"]
        )
        for row in Resource.objects.values("resource_type").annotate(count=_count("id")):
            resources.add_metric([row["resource_type"]], row["count"])
        yield resources

    def _indexing(self):
        from apps.embeddings.models import EmbeddingIndexState

        index = GaugeMetricFamily(
            "brainbox_embedding_index", "Embedding index state", labels=["status"]
        )
        for row in EmbeddingIndexState.objects.values("status").annotate(count=_count("document_id")):
            index.add_metric([row["status"]], row["count"])
        yield index

    def _git(self):
        from apps.git.models import GitRepository, GitSyncState

        repos = GaugeMetricFamily(
            "brainbox_git_repositories", "Git repositories", labels=["active"]
        )
        repos.add_metric(["true"], GitRepository.objects.filter(is_active=True).count())
        repos.add_metric(["false"], GitRepository.objects.filter(is_active=False).count())
        yield repos

        sync = GaugeMetricFamily(
            "brainbox_git_sync_state", "Git sync state", labels=["status"]
        )
        for row in (
            GitSyncState.objects.values("status").annotate(count=_count("repository_id"))
        ):
            sync.add_metric([row["status"]], row["count"])
        yield sync

        last_pull = GaugeMetricFamily(
            "brainbox_git_last_pull_timestamp_seconds", "Last successful git pull (epoch)"
        )
        for row in GitSyncState.objects.filter(last_pulled_at__isnull=False):
            last_pull.add_metric([], row.last_pulled_at.timestamp())
        yield last_pull

    def _secrets(self):
        from apps.secrets.models import Secret

        active = GaugeMetricFamily("brainbox_secrets", "Secrets", labels=["active"])
        active.add_metric(["true"], Secret.objects.filter(is_active=True).count())
        active.add_metric(["false"], Secret.objects.filter(is_active=False).count())
        yield active

    def _jobs(self):
        from apps.jobs.models import JobRun

        runs = GaugeMetricFamily("brainbox_job_runs", "Job runs by status", labels=["status"])
        for row in JobRun.objects.values("status").annotate(count=_count("id")):
            runs.add_metric([row["status"]], row["count"])
        yield runs

        duration = GaugeMetricFamily(
            "brainbox_job_run_duration_seconds", "Last run duration per job", labels=["job"]
        )
        for row in (
            JobRun.objects.filter(status=JobRun.Status.SUCCEEDED)
            .exclude(finished_at=None)
            .order_by("-finished_at")
            .values("job__name", "started_at", "finished_at")[:50]
        ):
            duration.add_metric(
                [row["job__name"]], (row["finished_at"] - row["started_at"]).total_seconds()
            )
        yield duration

    def _audit(self):
        from datetime import timedelta

        from django.utils import timezone

        from apps.audit.models import AuditEvent

        recent = GaugeMetricFamily(
            "brainbox_audit_events_24h", "Audit events in the last 24h", labels=["action"]
        )
        since = timezone.now() - timedelta(hours=24)
        for row in (
            AuditEvent.objects.filter(timestamp__gte=since)
            .values("action")
            .annotate(count=_count("id"))
        ):
            recent.add_metric([row["action"]], row["count"])
        yield recent

    # -- helpers -------------------------------------------------------------
    def _safe(self, group):
        try:
            yield from group()
        except Exception:  # noqa: BLE001 - never break a scrape
            logger.exception("metric collection failed")


def _count(field: str):
    from django.db.models import Count

    return Count(field)


def register() -> None:
    global _REGISTERED
    if REGISTRY is None or _REGISTERED:
        return
    REGISTRY.register(BrainboxCollector())
    _REGISTERED = True
