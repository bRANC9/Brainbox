"""Brainbox-specific job handlers (the app layer of the jobs framework)."""

from __future__ import annotations

import logging
from datetime import timedelta

from django.conf import settings
from django.utils import timezone

from .engine import recover_stuck_runs
from .registry import JobContext, register_job

logger = logging.getLogger("brainbox.jobs")


@register_job("embedding_backfill", "Reindex documents whose embedding index is missing or stale.")
def embedding_backfill(context: JobContext) -> dict:
    from apps.documents.models import Document
    from apps.embeddings.models import EmbeddingIndexState
    from apps.embeddings.services import IndexingService

    indexed = 0
    stale = 0
    for document in Document.objects.select_related("workspace", "project", "resource").iterator():
        state = EmbeddingIndexState.objects.filter(document=document).first()
        if state is None or state.status != EmbeddingIndexState.Status.INDEXED:
            IndexingService.index_document(document)
            indexed += 1
        else:
            # Cheap staleness check: reindex if the DB version moved ahead.
            if (state.version or 0) < (document.current_version or 0):
                IndexingService.index_document(document, force=True)
                stale += 1
    return {"indexed": indexed, "reindexed_stale": stale}


@register_job("git_sync_all", "Pull + import every active Git repository.")
def git_sync_all(context: JobContext) -> dict:
    from apps.git.models import GitRepository
    from apps.git.services import GitService

    results = {"ok": 0, "failed": 0, "documents": 0}
    for repository in GitRepository.objects.filter(is_active=True):
        if not repository.remote_url:
            continue
        try:
            counts = GitService.pull_repository(repository)
            results["ok"] += 1
            results["documents"] += sum(
                value for key, value in counts.items() if key.startswith("documents_")
            )
        except Exception as exc:  # noqa: BLE001 - one bad repo must not stop the rest
            results["failed"] += 1
            logger.warning("git sync failed for %s: %s", repository.pk, exc)
    return results


@register_job("recover_stuck_runs", "Reclaim job runs abandoned by a dead worker.")
def recover_stuck(context: JobContext) -> dict:
    return {"recovered": recover_stuck_runs()}


@register_job("prune_job_history", "Delete finished job runs older than the retention window.")
def prune_job_history(context: JobContext) -> dict:
    days = int(context.payload.get("days") or settings.BRAINBOX_JOB_HISTORY_DAYS)
    cutoff = timezone.now() - timedelta(days=days)
    from .models import JobRun, JobRunStatus

    deleted, _ = JobRun.objects.filter(
        created_at__lt=cutoff, status__in=[JobRunStatus.SUCCEEDED, JobRunStatus.FAILED, JobRunStatus.CANCELLED]
    ).delete()
    return {"deleted": deleted, "days": days}


@register_job("prune_audit_log", "Delete audit events older than the retention window.")
def prune_audit_log(context: JobContext) -> dict:
    from apps.audit.models import AuditEvent

    days = int(context.payload.get("days") or 365)
    deleted, _ = AuditEvent.objects.filter(timestamp__lt=timezone.now() - timedelta(days=days)).delete()
    return {"deleted": deleted, "days": days}
