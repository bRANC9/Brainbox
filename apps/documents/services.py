"""Application services for documents.

Files on disk are the source of truth; every write is mirrored into an
immutable :class:`DocumentVersion` snapshot for history/diff/restore.
"""

from __future__ import annotations

from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils.text import slugify

from apps.audit.models import AuditAction, AuditSource
from apps.audit.services import AuditService
from apps.resources.models import ResourceType
from apps.resources.services import ResourceService
from apps.resources.storage import get_storage

from .frontmatter import parse_frontmatter
from .models import ChangeSource, ChangeType, Document, DocumentStatus, DocumentVersion

_METADATA_KEYS = ("type", "domain", "owner", "tags", "source")


def _title_from_body(content: str) -> str:
    for line in content.splitlines():
        stripped = line.strip()
        if stripped.startswith("#"):
            return stripped.lstrip("#").strip()
    return ""


def _coerce_status(value) -> str | None:
    if not value:
        return None
    value = str(value).strip().lower()
    valid = {choice.value for choice in DocumentStatus}
    return value if value in valid else None


def _coerce_priority(value) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _normalize_path(path: str | None, title: str) -> str:
    path = (path or "").strip().lstrip("/")
    if not path:
        path = f"{slugify(title) or 'document'}.md"
    return path


class DocumentService:
    # -- reads ---------------------------------------------------------------
    @staticmethod
    def storage_path(document: Document):
        storage = get_storage()
        return storage.path_for(
            workspace_id=document.workspace_id,
            project_id=document.project_id,
            kind="documents",
            rel_path=document.path,
        )

    @staticmethod
    def read_content(document: Document) -> str:
        return get_storage().read_text(DocumentService.storage_path(document))

    # -- writes --------------------------------------------------------------
    @staticmethod
    @transaction.atomic
    def create(
        *,
        workspace,
        title: str = "",
        project=None,
        path: str | None = None,
        content: str = "",
        summary: str = "",
        metadata: dict | None = None,
        status: str | None = None,
        priority: int | None = None,
        created_by=None,
        source: str = ChangeSource.WEB,
        request=None,
        api_key=None,
    ) -> Document:
        content = content or ""
        frontmatter, _body = parse_frontmatter(content)

        meta = dict(metadata or {})
        for key in _METADATA_KEYS:
            if key in frontmatter and key not in meta:
                meta[key] = frontmatter[key]

        title = title or str(frontmatter.get("title") or "") or _title_from_body(content) or "Untitled"
        rel_path = _normalize_path(path, title)
        status = status or _coerce_status(frontmatter.get("status")) or DocumentStatus.DRAFT
        priority = priority if priority is not None else _coerce_priority(frontmatter.get("priority"))
        summary = summary or str(frontmatter.get("summary") or "")

        duplicate = Document.objects.filter(workspace=workspace, project=project, path=rel_path).exists()
        if duplicate:
            raise ValidationError({"path": f"A document already exists at '{rel_path}'."})

        parent = project.resource if project is not None else workspace.resource
        resource = ResourceService.create(
            resource_type=ResourceType.DOCUMENT,
            name=title,
            created_by=created_by,
            parent=parent,
            metadata={"workspace": str(workspace.pk), "project": str(project.pk) if project else None},
        )

        document = Document.objects.create(
            resource=resource,
            workspace=workspace,
            project=project,
            title=title,
            slug=(slugify(title) or "document")[:255],
            path=rel_path,
            summary=summary,
            status=status,
            priority=priority,
            frontmatter=frontmatter,
            metadata=meta,
            current_version=1,
            created_by=created_by,
        )

        storage = get_storage()
        storage.write_text(DocumentService.storage_path(document), content)
        DocumentVersion.objects.create(
            document=document,
            version=1,
            content=content,
            summary=summary,
            metadata=meta,
            change_type=ChangeType.CREATE,
            created_by=created_by,
            api_key=api_key,
            source=source,
        )

        AuditService.log(
            AuditAction.CREATE,
            user=created_by,
            api_key=api_key,
            resource=resource,
            workspace=workspace,
            project=project,
            source=AuditSource.API if request is not None else AuditSource.SYSTEM,
            request=request,
            version=1,
            detail={"type": "document", "path": rel_path, "title": title},
        )
        return document

    @staticmethod
    @transaction.atomic
    def update_content(
        *,
        document: Document,
        content: str,
        title: str | None = None,
        summary: str | None = None,
        metadata: dict | None = None,
        status: str | None = None,
        priority: int | None = None,
        user=None,
        source: str = ChangeSource.WEB,
        change_type: str = ChangeType.UPDATE,
        request=None,
        api_key=None,
        git_commit: str = "",
    ) -> Document:
        content = content or ""
        frontmatter, _body = parse_frontmatter(content)

        get_storage().write_text(DocumentService.storage_path(document), content)

        if title:
            document.title = title
            document.slug = (slugify(title) or "document")[:255]
        if summary is not None:
            document.summary = summary
        if status:
            document.status = status
        if priority is not None:
            document.priority = priority
        if metadata is not None:
            document.metadata = metadata
        document.frontmatter = frontmatter

        new_version = document.current_version + 1
        DocumentVersion.objects.create(
            document=document,
            version=new_version,
            content=content,
            summary=document.summary,
            metadata=document.metadata,
            change_type=change_type,
            created_by=user,
            api_key=api_key,
            source=source,
            git_commit=git_commit,
        )
        document.current_version = new_version
        document.save()

        AuditService.log(
            AuditAction.UPDATE,
            user=user,
            api_key=api_key,
            resource=document.resource,
            workspace=document.workspace,
            project=document.project,
            source=AuditSource.API if request is not None else AuditSource.SYSTEM,
            request=request,
            version=new_version,
            git_commit=git_commit,
            detail={"type": "document", "path": document.path},
        )
        return document

    @staticmethod
    @transaction.atomic
    def restore(*, document: Document, version_number: int, user=None, request=None, api_key=None):
        version = document.versions.get(version=version_number)
        return DocumentService.update_content(
            document=document,
            content=version.content,
            summary=version.summary,
            metadata=version.metadata,
            change_type=ChangeType.RESTORE,
            user=user,
            source=ChangeSource.WEB,
            request=request,
            api_key=api_key,
        )

    @staticmethod
    @transaction.atomic
    def delete(*, document: Document, user=None, request=None, api_key=None) -> None:
        storage = get_storage()
        path = DocumentService.storage_path(document)
        AuditService.log(
            AuditAction.DELETE,
            user=user,
            api_key=api_key,
            resource=document.resource,
            workspace=document.workspace,
            project=document.project,
            source=AuditSource.API if request is not None else AuditSource.SYSTEM,
            request=request,
            detail={"type": "document", "path": document.path},
        )
        storage.delete(path)
        document.resource.delete()
