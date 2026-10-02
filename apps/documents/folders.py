"""Folder organisation inside a workspace/project.

Folders are derived from (and kept consistent with) document paths, and created
on disk so git-backed vaults see them too. Nothing here stores knowledge - the
files stay the source of truth.

Since folders became permission-managed objects, they also carry a Resource whose
``parent`` mirrors the path hierarchy. That chain is what the permission engine
walks, which means three things this module is responsible for:

* a folder created at ``A/B/C`` gets resources for ``A``, ``A/B`` and ``A/B/C``,
  chained, so a grant on ``A/B/C`` inherits to its contents and its ancestors
  remain visible in the tree;
* :func:`rename_folder` renames the resource, :func:`move_folder` **re-parents**
  it - a folder that changes parent and keeps the old parent would keep the old
  access, which is the exact failure mode the whole model exists to prevent;
* :func:`delete_folder` deletes through the resource so the chain does not keep
  orphan nodes.
"""

from __future__ import annotations

from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import F, Value
from django.db.models.functions import Concat

from apps.resources.models import Resource, ResourceType
from apps.resources.services import ResourceService
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


def ancestor_paths(folder_path: str):
    """Every enclosing path of ``folder_path``, shallowest first."""
    parts = folder_path.split("/")
    for index in range(1, len(parts)):
        yield "/".join(parts[:index])


def _disk_path(workspace, project, folder_path: str):
    storage = get_storage()
    return storage.path_for(
        workspace_id=workspace.pk,
        project_id=project.pk if project else None,
        kind="documents",
        rel_path=folder_path,
    )


def _root_resource(workspace, project):
    return project.resource if project is not None else workspace.resource


def _parent_resource_for(workspace, project, folder_path: str):
    """The Resource a folder at ``folder_path`` hangs off."""
    parent_path = folder_path.rsplit("/", 1)[0] if "/" in folder_path else ""
    if parent_path:
        parent = (
            DocumentFolder.objects.select_related("resource")
            .filter(workspace=workspace, project=project, path=parent_path)
            .first()
        )
        if parent is not None and parent.resource_id:
            return parent.resource
    return _root_resource(workspace, project)


def resource_for_path(workspace, project, folder_path: str) -> Resource:
    """The folder's Resource, creating the folder (and its ancestors) if needed."""
    folder_path = normalize_folder_path(folder_path)
    if not folder_path:
        return _root_resource(workspace, project)
    return ensure_folder(workspace, project, folder_path).resource


def register_parents(workspace, project, document_path: str) -> None:
    """Auto-create any missing parent folders for a document path (idempotent)."""
    parts = (document_path or "").split("/")[:-1]
    for index in range(1, len(parts) + 1):
        ensure_folder(workspace, project, "/".join(parts[:index]))


@transaction.atomic
def ensure_folder(workspace, project, folder_path: str, created_by=None) -> DocumentFolder:
    folder_path = normalize_folder_path(folder_path)
    if not folder_path:
        raise ValidationError({"path": "Folder name is required."})
    for parent_path in ancestor_paths(folder_path):
        ensure_folder(workspace, project, parent_path, created_by=created_by)

    parent_resource = _parent_resource_for(workspace, project, folder_path)
    lookup = {"workspace": workspace, "project": project, "path": folder_path}
    folder = DocumentFolder.objects.filter(**lookup).first()
    if folder is not None and folder.resource_id:
        return folder
    if folder is not None:
        # A row from before folders became resource-backed; the migration fills
        # these in, this only matters for a database that skipped it.
        folder.resource = ResourceService.create(
            resource_type=ResourceType.FOLDER,
            name=folder_path.rsplit("/", 1)[-1],
            created_by=created_by or folder.created_by,
            parent=parent_resource,
            metadata={"path": folder_path},
        )
        folder.save(update_fields=["resource", "updated_at"])
        return folder

    resource = ResourceService.create(
        resource_type=ResourceType.FOLDER,
        name=folder_path.rsplit("/", 1)[-1],
        created_by=created_by,
        parent=parent_resource,
        metadata={"path": folder_path},
    )
    folder = DocumentFolder.objects.create(
        resource=resource,
        created_by=created_by,
        **lookup,
    )
    _disk_path(workspace, project, folder_path).mkdir(parents=True, exist_ok=True)
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
    if new_path == old_path:
        return folder

    DocumentFolder.objects.filter(
        workspace=folder.workspace, project=folder.project, path__startswith=f"{old_path}/"
    ).update(path=new_path + folder.path[len(old_path) :])
    folder.path = new_path
    folder.save(update_fields=["path", "updated_at"])
    # A rename that also re-parents (A/B -> X/B) has to move the Resource too.
    reparent_folder(folder)
    return folder


def reparent_folder(folder: DocumentFolder) -> None:
    """Re-point a folder's Resource at the folder that now encloses it.

    Renaming and moving both change which folder encloses a node; the Resource
    chain has to follow, or the node would silently keep the access of where it
    used to be. Subfolders keep their parent (this folder), so only this node's
    own parent changes.
    """
    parent = _parent_resource_for(folder.workspace, folder.project, folder.path)
    if parent is None or folder.resource.parent_id == parent.id:
        return
    folder.resource.parent = parent
    folder.resource.save(update_fields=["parent", "updated_at"])


@transaction.atomic
def move_folder(*, folder: DocumentFolder, new_parent: str = "", user=None) -> DocumentFolder:
    """Move a folder (and its whole subtree) under ``new_parent``.

    Documents and on-disk files below the folder are relocated too, so a
    drag-and-drop in the tree never leaves dangling paths.
    """
    from .models import Document

    parent = normalize_folder_path(new_parent)
    if parent and (parent == folder.path or parent.startswith(f"{folder.path}/")):
        raise ValidationError({"path": "A mappa nem fogható saját magába."})

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
        reparent_document(document)

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
    # A move nests the *whole* subpath, so the new parent chain is
    # "<parent>/<first segment>/..." and the intermediate rows may not exist yet.
    # Without them the re-parent below would silently fall back to the project.
    for ancestor in ancestor_paths(new_path):
        ensure_folder(folder.workspace, folder.project, ancestor, created_by=user)
    reparent_folder(folder)
    return folder


def reparent_document(document) -> None:
    """Point a document's Resource at the folder it now lives in.

    Without this a document keeps the permissions of the place it was *created*,
    so moving it into a tightly-scoped folder would not restrict it, and moving
    it out of one would not free it.
    """
    from .models import DocumentFolder as _Folder

    folder_path = (document.path or "").rsplit("/", 1)[0] if "/" in (document.path or "") else ""
    if not folder_path:
        parent = (
            document.project.resource if document.project_id else document.workspace.resource
        )
    else:
        folder = (
            _Folder.objects.select_related("resource")
            .filter(
                workspace=document.workspace,
                project=document.project,
                path=folder_path,
            )
            .first()
        )
        parent = folder.resource if folder is not None else (
            document.project.resource if document.project_id else document.workspace.resource
        )
    if parent is None or document.resource.parent_id == parent.id:
        return
    document.resource.parent = parent
    document.resource.save(update_fields=["parent", "updated_at"])


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
            reparent_document(document)
    else:
        has_documents = Document.objects.filter(
            workspace=folder.workspace, project=folder.project, path__startswith=folder.path + "/"
        ).exists()
        if has_documents:
            raise ValidationError(
                {"path": "Folder not empty. Move or delete its documents first."}
            )
        try:
            _disk_path(folder.workspace, folder.project, folder.path).rmdir()
        except OSError:
            pass
        # Deleting the Resource cascades to the folder and to any subfolder
        # Resources, so no orphan nodes are left on the chain.
        Resource.objects.filter(pk=folder.resource_id).delete()
        return
    Resource.objects.filter(pk=folder.resource_id).delete()
