"""Application services for documents.

Files on disk are the source of truth; every write is mirrored into an
immutable :class:`DocumentVersion` snapshot for history/diff/restore.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path

import yaml
from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone
from django.utils.text import slugify

from apps.audit.models import AuditAction, AuditSource
from apps.audit.services import AuditService
from apps.resources.models import ResourceType
from apps.resources.services import ResourceService
from apps.resources.storage import get_storage, resolve_path

from .frontmatter import parse_frontmatter
from .models import ChangeSource, ChangeType, Document, DocumentStatus, DocumentVersion

logger = logging.getLogger("brainbox.documents")

_METADATA_KEYS = ("type", "domain", "owner", "tags", "source")

_GIT_SOURCES = {ChangeSource.GIT, ChangeSource.IMPORT}


def _autocommit(document: Document, *, user, request, message: str | None = None) -> None:
    """Commit a Git-backed document back to its repository (best effort)."""
    try:
        from apps.git.services import GitService

        GitService.autocommit_document(
            document=document, user=user, request=request, message=message
        )
    except Exception:  # noqa: BLE001 - never break a document write because of Git
        logger.exception("git autocommit failed for document %s", document.pk)


def _reindex(document: Document) -> None:
    """Keep the vector index in sync with the source-of-truth file."""
    from apps.settings_store.services import get_value

    if not get_value("BRAINBOX_AUTO_INDEX", True):
        return
    try:
        from apps.embeddings.services import IndexingService

        IndexingService.index_document(document)
    except Exception:  # noqa: BLE001 - indexing must never break a write
        logger.exception("embedding index failed for document %s", document.pk)


def _deindex(document: Document) -> None:
    try:
        from apps.embeddings.services import IndexingService

        IndexingService.remove_document(document)
    except Exception:  # noqa: BLE001
        logger.exception("embedding de-index failed for document %s", document.pk)


def _rebuild_links(document: Document, *, user, request) -> None:
    """Keep [[wikilink]] / relative markdown links in sync with the content.

    The whole workspace/project scope is re-resolved so forward references
    (a link written before its target existed) also materialise.
    """
    try:
        from apps.links.services import LinkService

        LinkService.rebuild_all(
            workspace=document.workspace,
            project=document.project,
            user=user,
            request=request,
        )
    except Exception:  # noqa: BLE001 - link resolution must never break a write
        logger.exception("link rebuild failed for document %s", document.pk)


def _register_folders(document: Document) -> None:
    """Make sure the document's parent folders exist in the folder tree."""
    if "/" not in (document.path or ""):
        return
    try:
        from .folders import register_parents

        register_parents(document.workspace, document.project, document.path)
    except Exception:  # noqa: BLE001 - folder bookkeeping must not break a write
        logger.exception("folder registration failed for document %s", document.pk)


def _rebuild_deadlines(document: Document) -> None:
    """Re-extract deadlines mentioned in the document (files stay source of truth)."""
    try:
        from apps.deadlines.services import rebuild_deadlines

        rebuild_deadlines(document)
    except Exception:  # noqa: BLE001 - never break a write
        logger.exception("deadline extraction failed for document %s", document.pk)


def _scan_for_secrets(content: str) -> None:
    """Optionally warn/reject when content looks like it contains credentials."""
    from apps.settings_store.services import get_value

    mode = str(get_value("BRAINBOX_SECRET_SCAN_MODE", "off") or "off").lower()
    if mode not in {"warn", "reject"}:
        return
    try:
        from apps.secrets.scanner import scan_document_content

        findings = scan_document_content(content)
    except Exception:  # noqa: BLE001
        logger.exception("secret scan failed")
        return
    if not findings:
        return
    if mode == "reject":
        raise ValidationError(
            {"content": "Potential secrets detected. Store them in the Secret Vault instead."}
        )
    logger.warning(
        "potential secrets detected (%s); prefer the Secret Vault",
        ", ".join(finding["type"] for finding in findings),
    )


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
        return resolve_path(
            workspace=document.workspace,
            project=document.project,
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
        is_template: bool = False,
        created_by=None,
        source: str = ChangeSource.WEB,
        request=None,
        api_key=None,
    ) -> Document:
        content = content or ""
        _scan_for_secrets(content)
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
            is_template=is_template,
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
        if source not in _GIT_SOURCES:
            _autocommit(document, user=created_by, request=request)
        _rebuild_links(document, user=created_by, request=request)
        _rebuild_deadlines(document)
        _register_folders(document)
        _reindex(document)
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
        _scan_for_secrets(content)
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
        if source not in _GIT_SOURCES:
            _autocommit(document, user=user, request=request)
        _rebuild_links(document, user=user, request=request)
        _rebuild_deadlines(document)
        _register_folders(document)
        _reindex(document)
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
    def bulk_create_from_files(
        *,
        workspace,
        project=None,
        folder: str = "",
        uploads,
        created_by=None,
        source=ChangeSource.WEB,
        request=None,
        api_key=None,
    ) -> dict:
        """Create one Document per uploaded markdown file into a folder.

        ``uploads`` is an iterable of ``(filename, content_bytes)``. Each file
        lands at ``<folder>/<path>``, preserving any sub-paths the client sent
        (e.g. picking a whole ``dotnet/`` folder in the browser keeps the tree),
        with traversal segments stripped. Duplicates and unreadable files are
        reported instead of aborting the whole batch.
        """
        created = []
        skipped = []
        prefix = folder.strip("/")
        for name, data in uploads:
            raw = (name or "").strip().replace("\\", "/")
            raw = re.sub(r"^[A-Za-z]:", "", raw).lstrip("/")
            segments = [
                segment
                for segment in raw.split("/")
                if segment and segment not in {".", ".."}
            ]
            if not segments:
                continue
            safe_rel = "/".join(segments)
            try:
                content = data.decode("utf-8") if isinstance(data, (bytes, bytearray)) else str(data)
            except UnicodeDecodeError:
                skipped.append({"file": safe_rel, "reason": "nem utf-8 szöveg"})
                continue
            rel_path = f"{prefix}/{safe_rel}" if prefix else safe_rel
            stem = Path(safe_rel).stem
            for suffix in (".md", ".markdown", ".txt"):
                if stem.lower().endswith(suffix):
                    stem = stem[: -len(suffix)]
                    break
            title = stem.replace("_", " ").replace("-", " ").strip()
            try:
                document = DocumentService.create(
                    workspace=workspace,
                    project=project,
                    title=title or segments[-1],
                    path=rel_path,
                    content=content,
                    created_by=created_by,
                    source=source,
                    request=request,
                )
            except ValidationError as exc:
                skipped.append({"file": safe_rel, "reason": "; ".join(exc.messages)})
                continue
            created.append(document)
        return {"created": created, "skipped": skipped}

    @classmethod
    @transaction.atomic
    def instantiate_template(
        cls,
        *,
        template,
        workspace,
        project=None,
        title: str = "",
        path: str = "",
        folder: str = "",
        created_by=None,
        request=None,
        api_key=None,
        status: str = DocumentStatus.DRAFT,
    ):
        """Create a new document from a template, filling simple placeholders."""
        content = DocumentService.read_content(template)
        frontmatter, body = parse_frontmatter(content)
        # Templates are knowledge inputs, not published knowledge.
        frontmatter.pop("status", None)
        title = title or (str(frontmatter.get("title") or template.title).strip())
        body = (
            body.replace("{{title}}", title)
            .replace("{{date}}", timezone.now().date().isoformat())
        )
        rendered = body
        if frontmatter:
            rendered = (
                "---\n"
                + yaml.safe_dump(frontmatter, sort_keys=False, allow_unicode=True)
                + "---\n\n"
                + body.lstrip("\n")
            )
        rel_path = path or (
            f"{folder.strip('/')}/{slugify(title) or 'document'}.md"
            if folder
            else f"{slugify(title) or 'document'}.md"
        )
        return cls.create(
            workspace=workspace,
            project=project,
            title=title,
            path=rel_path,
            content=rendered,
            status=status,
            metadata={"created_from_template": str(template.pk), "template_title": template.title},
            created_by=created_by,
            source=ChangeSource.MCP if request is None else ChangeSource.API,
            request=request,
        )

    @classmethod
    @transaction.atomic
    def move(cls, document, new_path: str, *, user=None, request=None, api_key=None):
        """Move a document to another path (folder), renaming the file on disk."""
        from pathlib import Path as _Path

        new_path = (new_path or "").strip().lstrip("/")
        if not new_path:
            raise ValidationError({"path": "Path is required."})
        if new_path == document.path:
            return document

        storage = get_storage()
        old_file = cls.storage_path(document)
        new_file = storage.path_for(
            workspace_id=document.workspace_id,
            project_id=document.project_id,
            kind="documents",
            rel_path=new_path,
        )
        storage.ensure_parent(new_file)
        if old_file.exists():
            _Path(old_file).replace(new_file)
        document.path = new_path
        document.save(update_fields=["path", "updated_at"])

        _register_folders(document)
        _rebuild_links(document, user=user, request=request)
        _reindex(document)
        AuditService.log(
            AuditAction.UPDATE,
            user=user,
            api_key=api_key,
            resource=document.resource,
            workspace=document.workspace,
            project=document.project,
            source=AuditSource.API if request is not None else AuditSource.WEB,
            request=request,
            detail={"type": "document_move", "from": old_file.name, "to": new_path},
        )
        return document

    @classmethod
    @transaction.atomic
    def delete(cls, *, document: Document, user=None, request=None, api_key=None) -> None:
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
        _deindex(document)
        storage.delete(path)
        document.resource.delete()
