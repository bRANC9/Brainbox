"""Application services for binary/text files."""

from __future__ import annotations

import hashlib
import logging
import mimetypes

from django.core.exceptions import ValidationError
from django.db import transaction

from apps.audit.models import AuditAction, AuditSource
from apps.audit.services import AuditService
from apps.documents.models import ChangeSource, ChangeType
from apps.resources.models import ResourceType
from apps.resources.services import ResourceService
from apps.resources.storage import get_storage, resolve_path

from .models import File, FileVersion

logger = logging.getLogger("brainbox.files")

_GIT_SOURCES = {ChangeSource.GIT, ChangeSource.IMPORT}


def _checksum(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _autocommit(stored_file: File, *, user, request, message: str | None = None) -> None:
    try:
        from apps.git.services import GitService

        GitService.autocommit_file(
            stored_file=stored_file, user=user, request=request, message=message
        )
    except Exception:  # noqa: BLE001 - never break a file write because of Git
        logger.exception("git autocommit failed for file %s", stored_file.pk)


def _guess_mime(name: str, fallback: str = "") -> str:
    guessed, _ = mimetypes.guess_type(name)
    return guessed or fallback or "application/octet-stream"


def _normalize_path(path: str | None, name: str) -> str:
    path = (path or "").strip().lstrip("/")
    return path or name


class FileService:
    @staticmethod
    def storage_path(file: File):
        return resolve_path(
            workspace=file.workspace,
            project=file.project,
            kind="files",
            rel_path=file.path,
        )

    @staticmethod
    def read_bytes(file: File) -> bytes:
        return get_storage().read_bytes(FileService.storage_path(file))

    @staticmethod
    @transaction.atomic
    def create(
        *,
        workspace,
        name: str,
        data: bytes,
        project=None,
        path: str | None = None,
        mime_type: str = "",
        metadata: dict | None = None,
        created_by=None,
        source: str = ChangeSource.WEB,
        request=None,
        api_key=None,
    ) -> File:
        rel_path = _normalize_path(path, name)
        if File.objects.filter(workspace=workspace, project=project, path=rel_path).exists():
            raise ValidationError({"path": f"A file already exists at '{rel_path}'."})

        parent = project.resource if project is not None else workspace.resource
        resource = ResourceService.create(
            resource_type=ResourceType.FILE,
            name=name,
            created_by=created_by,
            parent=parent,
        )
        stored_file = File.objects.create(
            resource=resource,
            workspace=workspace,
            project=project,
            name=name,
            path=rel_path,
            mime_type=_guess_mime(name, mime_type),
            size=len(data),
            checksum=_checksum(data),
            metadata=metadata or {},
            current_version=1,
            created_by=created_by,
        )
        storage = get_storage()
        storage.write_bytes(FileService.storage_path(stored_file), data)
        FileVersion.objects.create(
            file=stored_file,
            version=1,
            size=stored_file.size,
            checksum=stored_file.checksum,
            storage_path=stored_file.path,
            change_type=ChangeType.CREATE,
            source=source,
            created_by=created_by,
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
            detail={"type": "file", "path": rel_path, "size": stored_file.size},
        )
        if source not in _GIT_SOURCES:
            _autocommit(stored_file, user=created_by, request=request)
        return stored_file

    @staticmethod
    @transaction.atomic
    def update_content(
        *,
        stored_file: File,
        data: bytes,
        user=None,
        source: str = ChangeSource.WEB,
        request=None,
        api_key=None,
    ) -> File:
        get_storage().write_bytes(FileService.storage_path(stored_file), data)
        stored_file.size = len(data)
        stored_file.checksum = _checksum(data)
        stored_file.current_version += 1
        stored_file.save()
        FileVersion.objects.create(
            file=stored_file,
            version=stored_file.current_version,
            size=stored_file.size,
            checksum=stored_file.checksum,
            storage_path=stored_file.path,
            change_type=ChangeType.UPDATE,
            source=source,
            created_by=user,
        )
        AuditService.log(
            AuditAction.UPDATE,
            user=user,
            api_key=api_key,
            resource=stored_file.resource,
            workspace=stored_file.workspace,
            project=stored_file.project,
            source=AuditSource.API if request is not None else AuditSource.SYSTEM,
            request=request,
            version=stored_file.current_version,
            detail={"type": "file", "path": stored_file.path},
        )
        if source not in _GIT_SOURCES:
            _autocommit(stored_file, user=user, request=request)
        return stored_file

    @staticmethod
    @transaction.atomic
    def delete(*, stored_file: File, user=None, request=None, api_key=None) -> None:
        storage = get_storage()
        path = FileService.storage_path(stored_file)
        AuditService.log(
            AuditAction.DELETE,
            user=user,
            api_key=api_key,
            resource=stored_file.resource,
            workspace=stored_file.workspace,
            project=stored_file.project,
            source=AuditSource.API if request is not None else AuditSource.SYSTEM,
            request=request,
            detail={"type": "file", "path": stored_file.path},
        )
        storage.delete(path)
        stored_file.resource.delete()
