"""Folder organisation inside a workspace/project.

Folders are derived from (and kept consistent with) document paths, and created
on disk so git-backed vaults see them too. Nothing here stores knowledge - the
files stay the source of truth.
"""

from __future__ import annotations

from django.core.exceptions import ValidationError
from django.db import transaction

from apps.resources.storage import get_storage

from .models import DocumentFolder

ILLEGAL_SEGMENTS = {"", ".", "..", "/", "\\"}


def normalize_folder_path(path: str) -> str:
    """Clean a user-supplied folder path (no leading/trailing slash, no '..')."""
    raw = (path or "").strip().replace("\\", "/")
    segments = [segment.strip() for segment in raw.split("/") if segment.strip()]
    for segment in segments:
        if segment in ILLEGAL_SEGMENTS or segment == "..":
            raise ValidationError({"path": f"Invalid folder name: {segment!r}"})
    return "/".join(segments)


def _disk_path(workspace, project, folder_path: str):
    storage = get_storage()
    return storage.path_for(
        workspace_id=workspace.pk,
        project_id=project.pk if project else None,
        kind="documents",
        rel_path=folder_path,
    )


def register_parents(workspace, project, document_path: str) -> None:
    """Auto-create any missing parent folders for a document path (idempotent)."""
    parts = (document_path or "").split("/")[:-1]
    for index in range(1, len(parts) + 1):
        ensure_folder(workspace, project, "/".join(parts[:index]))


@transaction.atomic
def ensure_folder(workspace, project, folder_path: str, created_by=None) -> DocumentFolder:
    folder_path = normalize_folder_path(folder_path)
    folder, created = DocumentFolder.objects.get_or_create(
        workspace=workspace,
        project=project,
        path=folder_path,
        defaults={"created_by": created_by},
    )
    if created:
        disk = _disk_path(workspace, project, folder_path)
        disk.mkdir(parents=True, exist_ok=True)
    return folder


@transaction.atomic
def create_folder(*, workspace, project=None, path: str, created_by=None) -> DocumentFolder:
    folder_path = normalize_folder_path(path)
    if not folder_path:
        raise ValidationError({"path": "Folder name is required."})
    return ensure_folder(workspace, project, folder_path, created_by=created_by)


@transaction.atomic
def rename_folder(*, folder: DocumentFolder, path: str) -> DocumentFolder:
    new_path = normalize_folder_path(path)
    if not new_path:
        raise ValidationError({"path": "Folder name is required."})
    old_path = folder.path
    DocumentFolder.objects.filter(
        workspace=folder.workspace, project=folder.project, path__startswith=f"{old_path}/"
    ).update(path=new_path + folder.path[len(old_path) :])
    folder.path = new_path
    folder.save(update_fields=["path", "updated_at"])
    return folder


@transaction.atomic
def delete_folder(*, folder: DocumentFolder, move_to_root: bool = False) -> None:
    """Delete an (empty) folder; with move_to_root its documents move up."""
    from .models import Document

    if move_to_root:
        prefix = folder.path + "/"
        for document in Document.objects.filter(
            workspace=folder.workspace, project=folder.project, path__startswith=prefix
        ):
            document.path = document.path[len(prefix) :]
            document.save(update_fields=["path"])
    else:
        has_documents = Document.objects.filter(
            workspace=folder.workspace, project=folder.project, path__startswith=folder.path + "/"
        ).exists()
        if has_documents:
            raise ValidationError(
                {"path": "Folder not empty. Move or delete its documents first."}
            )
        DocumentFolder.objects.filter(
            workspace=folder.workspace, project=folder.project, path=folder.path
        ).delete()
        try:
            _disk_path(folder.workspace, folder.project, folder.path).rmdir()
        except OSError:
            pass
        return
    DocumentFolder.objects.filter(
        workspace=folder.workspace, project=folder.project, path=folder.path
    ).delete()
