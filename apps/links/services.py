"""Application services for resource links.

Markdown/Obsidian references ([[wikilink]], relative [..](x.md)) are turned
into ResourceLink rows. Resolution happens per document, so creating or editing
a document keeps its links in sync (git scan calls the same code).
"""

from __future__ import annotations

import posixpath
import re
from pathlib import Path

from apps.audit.models import AuditAction, AuditSource
from apps.audit.services import AuditService

from .models import LinkType, ResourceLink

WIKILINK_RE = re.compile(r"!?\[\[([^\]]+)\]\]")
MDLINK_RE = re.compile(r"\[[^\]]*\]\(([^)]+)\)")


def extract_links(content: str) -> list[tuple[str, str]]:
    links: list[tuple[str, str]] = []
    for match in WIKILINK_RE.finditer(content or ""):
        links.append((match.group(1), LinkType.WIKILINK))
    for match in MDLINK_RE.finditer(content or ""):
        links.append((match.group(1), LinkType.REFERENCE))
    return links


def _index(documents) -> tuple[dict, dict, dict]:
    by_path: dict[str, object] = {}
    by_base: dict[str, object] = {}
    by_title: dict[str, object] = {}
    for document in documents:
        key = document.path.lower()
        if key.endswith(".md"):
            key = key[:-3]
        by_path[key] = document
        by_base.setdefault(Path(key).name, document)
        by_title.setdefault(document.title.strip().lower(), document)
    return by_path, by_base, by_title


def resolve_link(ref: str, source_document, by_path, by_base, by_title):
    ref = ref.split("|", 1)[0]
    ref = ref.split("#", 1)[0].strip()
    if not ref or "://" in ref:
        return None
    ref = ref.replace("\\", "/").strip()

    if ref.lower().endswith(".md"):
        if ref.startswith("/"):
            key = ref.lstrip("/")[:-3].lower()
        else:
            base = posixpath.dirname(source_document.path)
            key = posixpath.normpath(posixpath.join(base, ref)).lower()[:-3]
        return by_path.get(key) or by_base.get(Path(key).name)

    key = ref.lstrip("/")
    if key.lower().endswith(".md"):
        key = key[:-3]
    key = key.lower()
    return by_path.get(key) or by_base.get(Path(key).name) or by_title.get(ref.lower())


class LinkService:
    @staticmethod
    def create(*, source, target, link_type: str = LinkType.REFERENCE, created_by=None,
               request=None):
        link, _created = ResourceLink.objects.get_or_create(
            source=source,
            target=target,
            link_type=link_type,
            defaults={"created_by": created_by},
        )
        if _created:
            AuditService.log(
                AuditAction.UPDATE,
                user=created_by,
                resource=source,
                source=AuditSource.API if request is not None else AuditSource.SYSTEM,
                request=request,
                detail={"type": "resource_link", "target": str(target.id), "link_type": link_type},
            )
        return link

    @staticmethod
    def delete(*, source, target=None, link_type: str | None = None) -> int:
        queryset = ResourceLink.objects.filter(source=source)
        if target is not None:
            queryset = queryset.filter(target=target)
        if link_type:
            queryset = queryset.filter(link_type=link_type)
        deleted, _ = queryset.delete()
        return deleted

    @classmethod
    def rebuild_document_links(cls, *, document, user=None, request=None) -> int:
        """Recreate the outgoing links of a single document from its content."""
        from apps.documents.models import Document
        from apps.documents.services import DocumentService

        siblings = list(
            Document.objects.filter(
                workspace_id=document.workspace_id, project_id=document.project_id
            )
        )
        by_path, by_base, by_title = _index(siblings)

        ResourceLink.objects.filter(source=document.resource).delete()
        try:
            content = DocumentService.read_content(document)
        except Exception:  # noqa: BLE001 - file may be missing
            return 0

        created = 0
        for ref, link_type in extract_links(content):
            target = resolve_link(ref, document, by_path, by_base, by_title)
            if target is None or target.pk == document.pk:
                continue
            cls.create(
                source=document.resource,
                target=target.resource,
                link_type=link_type,
                created_by=user,
                request=request,
            )
            created += 1
        return created

    @classmethod
    def rebuild_all(cls, *, workspace, project=None, user=None, request=None) -> int:
        from apps.documents.models import Document

        total = 0
        documents = Document.objects.filter(
            workspace=workspace, project=project
        ).select_related("resource", "workspace", "project")
        for document in documents:
            total += cls.rebuild_document_links(
                document=document, user=user, request=request
            )
        return total
