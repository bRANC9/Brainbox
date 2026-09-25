"""Phase 7 services: knowledge graph, AI drafts, quality metrics, discovery."""

from __future__ import annotations

from datetime import timedelta

from django.conf import settings
from django.db.models import Count
from django.utils import timezone

from apps.audit.models import AuditAction, AuditSource
from apps.audit.services import AuditService
from apps.documents.models import ChangeSource, ChangeType, Document, DocumentStatus
from apps.documents.services import DocumentService
from apps.embeddings.models import KnowledgeChunk
from apps.links.models import ResourceLink
from apps.permissions.constants import Permission
from apps.permissions.services import PermissionService
from apps.resources.models import Resource

from .llm import get_llm_provider


def _may_read(user, resource, api_key=None) -> bool:
    return PermissionService.check(user, resource, Permission.READ, api_key=api_key)


class DiscoveryService:
    """Agent-facing discovery of skills/patterns/conventions/decisions/examples."""

    TYPE_FOLDERS = {
        "skill": ["skills/"],
        "pattern": ["patterns/"],
        "convention": ["conventions/"],
        "decision": ["decisions/"],
        "example": ["examples/"],
    }

    @classmethod
    def _matches(cls, document: Document, type_name: str) -> bool:
        if (document.metadata or {}).get("type") == type_name:
            return True
        if (document.frontmatter or {}).get("type") == type_name:
            return True
        path = (document.path or "").lower()
        return any(folder in path for folder in cls.TYPE_FOLDERS.get(type_name, []))

    @classmethod
    def discover(cls, user, *, api_key=None, types=None, workspace_id=None, limit=50) -> dict:
        queryset = Document.objects.select_related("workspace", "project", "resource")
        if workspace_id:
            queryset = queryset.filter(workspace_id=workspace_id)
        types = types or list(cls.TYPE_FOLDERS)

        buckets: dict[str, list[dict]] = {name: [] for name in types}
        for document in queryset:
            if not _may_read(user, document.resource, api_key):
                continue
            for type_name in types:
                if cls._matches(document, type_name):
                    buckets[type_name].append(
                        {
                            "document_id": str(document.pk),
                            "resource_id": str(document.resource_id),
                            "title": document.title,
                            "path": document.path,
                            "status": document.status,
                            "priority": document.priority,
                            "summary": document.summary,
                            "workspace": str(document.workspace_id),
                        }
                    )
        for items in buckets.values():
            items.sort(key=lambda row: (row["status"] != DocumentStatus.APPROVED, -row["priority"]))
            del items[limit:]
        return buckets


class GraphService:
    """BFS over ResourceLink, always permission-filtered."""

    @classmethod
    def neighbors(
        cls, user, resource: Resource, *, api_key=None, depth: int = 1, limit: int = 25
    ) -> list[dict]:
        seen = {resource.id}
        frontier = [resource]
        results: list[dict] = []

        for _level in range(max(1, min(depth, 3))):
            next_frontier = []
            for node in frontier:
                links = list(
                    ResourceLink.objects.filter(source=node).select_related("target")
                ) + list(ResourceLink.objects.filter(target=node).select_related("source"))
                for link in links:
                    other = link.target if link.source_id == node.id else link.source
                    if other.id in seen:
                        continue
                    seen.add(other.id)
                    accessible = _may_read(user, other, api_key)
                    results.append(
                        {
                            "resource_id": str(other.id),
                            "name": other.name,
                            "type": other.resource_type,
                            "link_type": link.link_type,
                            "accessible": accessible,
                            "summary_visible": bool((other.metadata or {}).get("public_summary")),
                        }
                    )
                    if accessible:
                        next_frontier.append(other)
                    if len(results) >= limit:
                        return results
            frontier = next_frontier
            if not frontier:
                break
        return results


class DraftService:
    """AI-generated knowledge is always created as DRAFT (terv.md 19)."""

    @classmethod
    def generate_draft(
        cls, *, workspace, project=None, title: str, prompt: str, user, request=None
    ) -> Document:
        from apps.search.services import SearchService

        context_results = SearchService.search(
            user, prompt, workspace_id=workspace.pk, project_id=project.pk if project else None, limit=3
        )
        context_text = "\n".join(
            f"- {row['title']} ({row['path']}): {row['snippet']}" for row in context_results
        )

        provider = get_llm_provider()
        content = provider.generate(title=title, prompt=prompt, context=context_text)

        return DocumentService.create(
            workspace=workspace,
            project=project,
            title=title,
            content=content,
            summary=f"AI draft for: {prompt[:160]}",
            status=DocumentStatus.DRAFT,
            metadata={"ai_generated": True, "prompt": prompt[:500]},
            created_by=user,
            source=ChangeSource.MCP if request is None else ChangeSource.API,
            request=request,
        )

    @classmethod
    def set_status(
        cls, *, document: Document, status: str, user, request=None, api_key=None
    ) -> Document:
        if status not in {choice for choice, _ in DocumentStatus.choices}:
            raise ValueError(f"Invalid status: {status}")
        previous = document.status
        document.status = status
        document.save(update_fields=["status", "updated_at"])
        DocumentService.update_content(
            document=document,
            content=DocumentService.read_content(document),
            change_type=ChangeType.UPDATE,
            user=user,
            request=request,
            api_key=api_key,
        )
        AuditService.log(
            AuditAction.UPDATE,
            user=user,
            api_key=api_key,
            resource=document.resource,
            workspace=document.workspace,
            project=document.project,
            source=AuditSource.API if request is not None else AuditSource.SYSTEM,
            request=request,
            detail={"type": "status_change", "from": previous, "to": status},
        )
        return document


class QualityService:
    @classmethod
    def metrics(cls) -> dict:
        cutoff = timezone.now() - timedelta(days=settings.BRAINBOX_STALE_DAYS)
        total = Document.objects.count()
        by_status = {
            row["status"]: row["count"]
            for row in Document.objects.values("status").annotate(count=Count("resource_id"))
        }
        linked_ids = set(
            ResourceLink.objects.values_list("source_id", flat=True)
        ) | set(ResourceLink.objects.values_list("target_id", flat=True))
        orphans = [
            {"id": str(document.pk), "title": document.title, "path": document.path}
            for document in Document.objects.exclude(pk__in=linked_ids)
        ]
        stale = [
            {"id": str(document.pk), "title": document.title, "updated_at": document.updated_at}
            for document in Document.objects.filter(updated_at__lt=cutoff)
        ]
        approved = by_status.get(DocumentStatus.APPROVED, 0)
        return {
            "documents": total,
            "by_status": by_status,
            "approved_ratio": round(approved / total, 3) if total else 0.0,
            "chunks": KnowledgeChunk.objects.count(),
            "links": ResourceLink.objects.count(),
            "orphans": orphans,
            "stale": stale,
            "workspaces": Document.objects.values("workspace_id").distinct().count(),
        }
