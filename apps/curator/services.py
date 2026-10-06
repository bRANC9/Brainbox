"""The curator: scan the tree for drift and write reviewable proposals.

Every detector is deterministic - an exact content match, a passed review date -
so the finding is a fact, not a guess. Applying an approved proposal goes through
the same services the UI and the API use, so nothing bypasses the permission
engine, the audit trail or the version history.
"""

from __future__ import annotations

import hashlib
from datetime import date

from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from apps.audit.models import AuditAction, AuditSource
from apps.audit.services import AuditService
from apps.documents.models import Document, DocumentStatus
from apps.documents.services import DocumentService
from apps.knowledge.services import DraftService
from apps.links.models import LinkType
from apps.links.services import LinkService
from apps.permissions.constants import Permission
from apps.permissions.services import PermissionService

from .models import CuratorKind, CuratorProposal, CuratorStatus

DEFAULT_SCAN_LIMIT = 500


def _parse_date(value):
    if not value:
        return None
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


class CuratorService:
    # -- scanning ------------------------------------------------------------
    @classmethod
    def scan_workspace(cls, workspace, *, limit: int = DEFAULT_SCAN_LIMIT) -> dict:
        """Find drift in one workspace and upsert a proposal per finding."""
        findings = cls._duplicates(workspace, limit) + cls._stale(workspace)
        seen = {(finding["kind"], finding["signature"]) for finding in findings}

        # A finding that no longer exists closes its proposal rather than
        # lingering as something to decide.
        open_here = CuratorProposal.objects.filter(
            workspace=workspace, status=CuratorStatus.OPEN
        )
        if seen:
            keep = Q()
            for kind, signature in seen:
                keep |= Q(kind=kind, signature=signature)
            resolved = open_here.exclude(keep).update(
                status=CuratorStatus.STALE, updated_at=timezone.now()
            )
        else:
            resolved = open_here.update(
                status=CuratorStatus.STALE, updated_at=timezone.now()
            )

        created = refreshed = reopened = 0
        for finding in findings:
            proposal, was_created = CuratorProposal.objects.get_or_create(
                workspace=workspace,
                kind=finding["kind"],
                signature=finding["signature"],
                defaults={
                    "resource": finding["resource"],
                    "title": finding["title"],
                    "rationale": finding["rationale"],
                    "payload": finding["payload"],
                    "status": CuratorStatus.OPEN,
                },
            )
            if was_created:
                created += 1
                continue
            if proposal.status in {CuratorStatus.REJECTED, CuratorStatus.APPLIED}:
                # A human already decided this one; do not reopen it.
                continue
            if proposal.status == CuratorStatus.STALE:
                reopened += 1
            unchanged = (
                proposal.title == finding["title"]
                and proposal.rationale == finding["rationale"]
                and proposal.payload == finding["payload"]
                and proposal.status == CuratorStatus.OPEN
            )
            proposal.title = finding["title"]
            proposal.rationale = finding["rationale"]
            proposal.payload = finding["payload"]
            proposal.status = CuratorStatus.OPEN
            proposal.save(update_fields=["title", "rationale", "payload", "status", "updated_at"])
            if not unchanged:
                refreshed += 1

        return {
            "created": created,
            "refreshed": refreshed,
            "reopened": reopened,
            "resolved": resolved,
            "open": CuratorProposal.objects.filter(
                workspace=workspace, status=CuratorStatus.OPEN
            ).count(),
        }

    @classmethod
    def scan_all(cls, *, limit: int = DEFAULT_SCAN_LIMIT) -> dict:
        from apps.workspaces.models import Workspace

        totals = {"workspaces": 0, "created": 0, "refreshed": 0, "resolved": 0}
        for workspace in Workspace.objects.all().iterator():
            result = cls.scan_workspace(workspace, limit=limit)
            totals["workspaces"] += 1
            totals["created"] += result["created"]
            totals["refreshed"] += result["refreshed"]
            totals["resolved"] += result["resolved"]
        return totals

    @classmethod
    def _duplicates(cls, workspace, limit: int) -> list[dict]:
        """Documents with byte-identical content, canonicalised on the oldest."""
        documents = list(
            Document.objects.filter(workspace=workspace)
            .exclude(status=DocumentStatus.ARCHIVED)
            .select_related("resource")
            .order_by("created_at")[:limit]
        )
        by_hash: dict[str, list] = {}
        for document in documents:
            try:
                content = DocumentService.read_content(document)
            except Exception:  # noqa: BLE001 - a missing file is not a finding
                continue
            if not content or not content.strip():
                continue
            digest = hashlib.sha256(content.encode("utf-8")).hexdigest()
            by_hash.setdefault(digest, []).append(document)

        findings: list[dict] = []
        for group in by_hash.values():
            if len(group) < 2:
                continue
            canonical = group[0]
            for duplicate in group[1:]:
                findings.append(
                    {
                        "kind": CuratorKind.DUPLICATE,
                        "signature": f"dup:{canonical.pk}:{duplicate.pk}",
                        "resource": duplicate.resource,
                        "title": f"Duplikátum: {duplicate.title}",
                        "rationale": (
                            f"A(z) „{duplicate.path}” tartalma megegyezik a(z) "
                            f"„{canonical.path}” dokumentuméval."
                        ),
                        "payload": {
                            "canonical_id": str(canonical.pk),
                            "duplicate_id": str(duplicate.pk),
                        },
                    }
                )
                if len(findings) >= limit:
                    return findings
        return findings

    @classmethod
    def _stale(cls, workspace) -> list[dict]:
        """Approved documents whose frontmatter review_by is in the past."""
        today = timezone.now().date()
        findings: list[dict] = []
        for document in (
            Document.objects.filter(
                workspace=workspace, status=DocumentStatus.APPROVED
            ).select_related("resource")
        ):
            review_by = _parse_date((document.frontmatter or {}).get("review_by"))
            if review_by is None or review_by >= today:
                continue
            findings.append(
                {
                    "kind": CuratorKind.STALE,
                    "signature": f"stale:{document.pk}:{review_by.isoformat()}",
                    "resource": document.resource,
                    "title": f"Lejárt ellenőrzés: {document.title}",
                    "rationale": (
                        f"A(z) „{document.path}” ellenőrzése lejárt "
                        f"({review_by.isoformat()})."
                    ),
                    "payload": {
                        "document_id": str(document.pk),
                        "review_by": review_by.isoformat(),
                    },
                }
            )
        return findings

    # -- deciding ------------------------------------------------------------
    @classmethod
    def can_decide(cls, user, proposal, *, api_key=None) -> bool:
        resource = proposal.resource or proposal.workspace.resource
        return PermissionService.check(user, resource, Permission.WRITE, api_key=api_key)

    @classmethod
    @transaction.atomic
    def decide(cls, proposal, *, approve: bool, user, request=None, api_key=None) -> CuratorProposal:
        if proposal.status not in {CuratorStatus.OPEN, CuratorStatus.STALE}:
            raise ValidationError({"status": "Ezt a javaslatot már elbírálták."})
        if not cls.can_decide(user, proposal, api_key=api_key):
            raise ValidationError({"permission": "Nincs jogosultságod elbírálni."})

        if approve:
            cls._apply(proposal, user=user, request=request)
            proposal.status = CuratorStatus.APPLIED
        else:
            proposal.status = CuratorStatus.REJECTED
        proposal.decided_by = user
        proposal.decided_at = timezone.now()
        proposal.save(update_fields=["status", "decided_by", "decided_at", "updated_at"])
        AuditService.log(
            AuditAction.UPDATE,
            user=user,
            api_key=api_key,
            resource=proposal.resource,
            source=AuditSource.API if request is not None else AuditSource.SYSTEM,
            request=request,
            detail={
                "type": "curator_proposal",
                "kind": proposal.kind,
                "title": proposal.title,
                "decision": "applied" if approve else "rejected",
            },
        )
        return proposal

    @classmethod
    def _apply(cls, proposal, *, user, request) -> None:
        if proposal.kind == CuratorKind.DUPLICATE:
            duplicate = Document.objects.filter(
                pk=proposal.payload.get("duplicate_id")
            ).first()
            canonical = Document.objects.filter(
                pk=proposal.payload.get("canonical_id")
            ).first()
            if duplicate is None:
                return
            DraftService.set_status(
                document=duplicate, status=DocumentStatus.ARCHIVED, user=user, request=request
            )
            if canonical is not None:
                LinkService.create(
                    source=duplicate.resource,
                    target=canonical.resource,
                    link_type=LinkType.RELATED,
                    created_by=user,
                    request=request,
                )
        elif proposal.kind == CuratorKind.STALE:
            document = Document.objects.filter(
                pk=proposal.payload.get("document_id")
            ).first()
            if document is not None:
                # Stale approved knowledge goes back to draft: it must be looked
                # at before it is trusted again.
                DraftService.set_status(
                    document=document, status=DocumentStatus.DRAFT, user=user, request=request
                )
