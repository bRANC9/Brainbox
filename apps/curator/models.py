"""Knowledge curator: the system proposes tidy-ups, a human disposes.

A knowledge base drifts - duplicates appear, approved pages go stale, orphans
pile up. The curator scans for those and writes a *proposal* for each, never a
silent edit: approving one goes through the same services the rest of the
platform uses, so the ACL, the audit trail and the version history all hold. A
rejected proposal is remembered so the scan does not nag with the same finding.
"""

from __future__ import annotations

import uuid

from django.conf import settings
from django.db import models


class CuratorKind(models.TextChoices):
    DUPLICATE = "duplicate", "Exact duplicate"
    STALE = "stale", "Stale (review due)"


class CuratorStatus(models.TextChoices):
    OPEN = "open", "Open"
    APPLIED = "applied", "Applied"
    REJECTED = "rejected", "Rejected"
    STALE = "stale", "No longer found"


class CuratorProposal(models.Model):
    """One suggested change, reviewable and reversible.

    ``resource`` is the document the change acts on, so who may decide is the
    same ACL question as who may edit that document. ``signature`` is a stable
    string for the finding (``dup:<canonical>:<dup>``), which keeps a rescan from
    piling up duplicates of the proposal itself.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    workspace = models.ForeignKey(
        "workspaces.Workspace", on_delete=models.CASCADE, related_name="curator_proposals"
    )
    resource = models.ForeignKey(
        "resources.Resource",
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="curator_proposals",
    )
    kind = models.CharField(max_length=32, choices=CuratorKind.choices)
    signature = models.CharField(max_length=256)
    title = models.CharField(max_length=512)
    rationale = models.TextField(blank=True)
    payload = models.JSONField(default=dict, blank=True)
    status = models.CharField(
        max_length=16, choices=CuratorStatus.choices, default=CuratorStatus.OPEN, db_index=True
    )
    decided_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="curator_decisions",
    )
    decided_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "curator_proposal"
        ordering = ["-created_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["workspace", "kind", "signature"],
                name="uniq_curator_proposal",
            )
        ]

    def __str__(self) -> str:
        return f"{self.kind}:{self.title}"
