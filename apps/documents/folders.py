"""Folder organisation inside a workspace/project.

Folders are derived from (and kept consistent with) document paths, and created
on disk so git-backed vaults see them too. Nothing here stores knowledge - the
files stay the source of truth.
"""

from __future__ import annotations

from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import F, Value
from django.db.models.functions import Concat

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
def move_folder(*, folder: DocumentFolder, new_parent: str = "", user=None) -> DocumentFolder:
    """Move a folder (and its whole subtree) under ``new_parent``.

    Documents and on-disk files below the folder are relocated too, so a
    drag-and-drop in the tree never leaves dangling paths.
    """
    from .models import Document

    parent = normalize_folder_path(new_parent)
    if parent and (parent == folder.path or parent.startswith(f"{folder.path}/")):
        raise ValidationError({"path": "A mappa nem mogatható saját magába."})

    old_path = folder.path
    new_path = f"{parent}/{old_path}" if parent else old_path
    if new_path == old_path:
        return folder

    storage = get_storage()
    for document in Document.objects.filter(
        workspace=folder.workspace, project=folder.project, path__startswith=f"{old_path}/"
    ):
        target = new_path + document.path[len(old_path) :]
        source = storage.path_for(
            workspace_id=folder.workspace.pk,
            project_id=folder.project.pk if folder.project else None,
            kind="documents",
            rel_path=document.path,
        )
        destination = storage.path_for(
            workspace_id=folder.workspace.pk,
            project_id=folder.project.pk if folder.project else None,
            kind="documents",
            rel_path=target,
        )
        storage.ensure_parent(destination)
        if source.exists():
            source.replace(destination)
        document.path = target
        document.save(update_fields=["path"])

    # Empty dirs left behind by the move.
    old_disk = _disk_path(folder.workspace, folder.project, old_path)
    if old_disk.exists():
        try:
            old_disk.rmdir()
        except OSError:
            pass

    DocumentFolder.objects.filter(
        workspace=folder.workspace,
        project=folder.project,
        path__startswith=f"{old_path}/",
    ).exclude(path=old_path).update(
        path=Concat(Value(new_path), F("path")[len(old_path) :])
    )
    folder.path = new_path
    folder.save(update_fields=["path", "updated_at"])
    if parent:
        ensure_folder(folder.workspace, folder.project, parent, created_by=user)
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
