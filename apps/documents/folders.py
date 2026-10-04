"""Folder organisation inside the knowledge tree.

Folders are named nodes chained by ``container`` (another folder, a project or a
workspace). ``path`` stays as a denormalised cache because every list view sorts
and filters on it, and because the on-disk layout is still addressed by it - but
:meth:`DocumentFolder.recompute_path` is the only thing that writes it.

What changed when folders became real objects:

* **A rename is one field.** It used to rewrite the ``path`` of every descendant;
  now the container chain moves the node and each descendant recomputes itself.
* **A folder has an owner** and inherits its container's ACL like any other node,
  so a grant on a deep folder behaves the same as on a project.
* **Nesting is not constrained.** A folder can hang off any container, so the
  "inside a project or at the workspace root" rule is gone - it was always
  allowed in practice, it just had no name to be given.
"""

from __future__ import annotations

from django.core.exceptions import ValidationError
from django.db import transaction

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


def _disk_path(workspace, project, folder_path: str):
    storage = get_storage()
    return storage.path_for(
        workspace_id=workspace.pk,
        project_id=project.pk if project else None,
        kind="documents",
        rel_path=folder_path,
    )


def _file_disk_path(workspace, project, folder_path: str):
    storage = get_storage()
    return storage.path_for(
        workspace_id=workspace.pk,
        project_id=project.pk if project else None,
        kind="files",
        rel_path=folder_path,
    )


def ancestor_paths(folder_path: str):
    """Every enclosing path of ``folder_path``, shallowest first."""
    parts = folder_path.split("/")
    for index in range(1, len(parts)):
        yield "/".join(parts[:index])


def scope_resource(workspace, project) -> Resource:
    """The node that holds top-level folders of this scope."""
    return project.resource if project is not None else workspace.resource


def _scope_for_container(container: Resource):
    """(workspace, project) owning ``container``, read off the node itself.

    Derived from the container rather than from the folder row, which is what
    makes nesting unconstrained: a folder knows where it lives because its
    container knows.
    """
    if container is None:
        return None, None
    project = getattr(container, "project", None)
    if project is not None:
        return project.workspace, project
    workspace = getattr(container, "workspace", None)
    if workspace is not None:
        return workspace, None
    return None, None


def _validate_name(name: str) -> str:
    """A single path segment, checked properly.

    ``ILLEGAL_SEGMENTS`` contains the empty string (for the path-splitting case),
    and ``"" in "anything"`` is always true - so membership cannot be used for a
    segment check. A name is valid when it is non-empty, is not ``.``/``..`` and
    carries no separator.
    """
    name = (name or "").strip()
    if not name or name in {".", ".."}:
        raise ValidationError({"name": "A mappa neve kötelező."})
    if "/" in name or "\\" in name or "\x00" in name:
        raise ValidationError({"name": f"Invalid folder name: {name!r}"})
    return name[:255]


@transaction.atomic
def create_folder_node(
    *,
    workspace,
    project=None,
    parent: DocumentFolder | None = None,
    name: str,
    description: str = "",
    owner=None,
    created_by=None,
) -> DocumentFolder:
    """Create a folder as a named node under ``parent`` (or the scope root)."""
    name = _validate_name(name)

    if parent is not None:
        container = parent.resource
    else:
        container = scope_resource(workspace, project)

    if DocumentFolder.objects.filter(container=container, name=name).exists():
        raise ValidationError({"name": f"„{name}” már létezik itt."})

    resource = ResourceService.create(
        resource_type=ResourceType.FOLDER,
        name=name,
        created_by=created_by,
        parent=container,
        metadata={"name": name},
    )
    folder = DocumentFolder.objects.create(
        resource=resource,
        container=container,
        name=name,
        description=description or "",
        workspace=workspace,
        project=project,
        path="",  # filled by recompute_path below
        owner=owner or created_by,
        created_by=created_by,
    )
    folder.recompute_path()

    _disk_path(workspace, project, folder.path).mkdir(parents=True, exist_ok=True)
    return folder


@transaction.atomic
def ensure_folder(workspace, project, folder_path: str, created_by=None) -> DocumentFolder:
    """Get-or-create the folder at ``folder_path``, creating ancestors too.

    Still takes a path, because that is what the callers have: document paths
    from a create, an import, or a git sync. The path is walked one segment at a
    time so each step is a proper named node rather than a row invented from a
    string.
    """
    folder_path = normalize_folder_path(folder_path)
    if not folder_path:
        raise ValidationError({"path": "Folder name is required."})

    container = scope_resource(workspace, project)
    walked: list[DocumentFolder] = []
    for segment in [s for s in folder_path.split("/") if s]:
        existing = (
            DocumentFolder.objects.filter(container=container, name=segment)
            .select_related("container")
            .first()
        )
        if existing is None:
            parent = walked[-1] if walked else None
            existing = create_folder_node(
                workspace=workspace,
                project=project,
                parent=parent,
                name=segment,
                created_by=created_by,
            )
        walked.append(existing)
        container = existing.resource

    return walked[-1]


@transaction.atomic
def create_folder(*, workspace, project=None, path: str, created_by=None) -> DocumentFolder:
    folder_path = normalize_folder_path(path)
    if not folder_path:
        raise ValidationError({"path": "Folder name is required."})
    return ensure_folder(workspace, project, folder_path, created_by=created_by)


@transaction.atomic
def rename_folder(*, folder: DocumentFolder, path: str) -> DocumentFolder:
    """Rename a folder; the container chain moves it and descendants recompute.

    ``path`` may still be a full path (the tree's inline rename passes one), in
    which case the last segment is the new name and the rest is ignored: the
    parent comes from the chain now, not from the string.
    """
    new_path = normalize_folder_path(path)
    if not new_path:
        raise ValidationError({"path": "Folder name is required."})
    new_name = new_path.rsplit("/", 1)[-1]
    new_name = _validate_name(new_name)
    if new_name == folder.name:
        return folder

    old_path = folder.path
    if DocumentFolder.objects.filter(
        container=folder.container, name=new_name
    ).exclude(pk=folder.pk).exists():
        raise ValidationError({"name": f"„{new_name}” már létezik itt."})

    workspace, project = folder.workspace, folder.project
    _move_disk(workspace, project, old_path, folder)

    folder.name = new_name
    folder.resource.name = new_name
    folder.resource.metadata = {**(folder.resource.metadata or {}), "name": new_name}
    folder.save(update_fields=["name", "updated_at"])
    folder.resource.save(update_fields=["name", "metadata", "updated_at"])
    folder.recompute_path()
    rewrite_descendants(folder)
    return folder


@transaction.atomic
def move_folder(
    *,
    folder: DocumentFolder,
    new_parent: DocumentFolder | str | None = None,
    user=None,
) -> DocumentFolder:
    """Re-parent a folder. Its subtree follows, because paths recompute.

    ``new_parent`` may be a folder node or a path string: the drag-and-drop
    endpoint has a path, the API and the tests have a node, and making them
    resolve between the two here is better than making every caller translate.
    """
    if isinstance(new_parent, str):
        new_parent = resolve_folder_path(folder, new_parent) if new_parent.strip() else None

    if new_parent is not None:
        if new_parent.pk == folder.pk:
            raise ValidationError({"path": "A mappa nem fogható saját magába."})
        for ancestor in new_parent.chain():
            if ancestor.pk == folder.pk:
                raise ValidationError({"path": "Nem moztható a saját leszármazottjába."})

    old_path = folder.path
    if DocumentFolder.objects.filter(
        container=new_parent.resource if new_parent else folder.container,
        name=folder.name,
    ).exclude(pk=folder.pk).exists():
        raise ValidationError({"name": f"„{folder.name}” már létezik az új helyen."})

    _move_disk(folder.workspace, folder.project, old_path, folder, new_parent=new_parent)
    container = new_parent.resource if new_parent else folder.container
    folder.container = container
    folder.save(update_fields=["container", "updated_at"])
    # The Resource chain is what the permission engine walks, so it has to follow
    # the container. Forgetting this leaves the folder holding its *old* parent's
    # access after a move.
    if folder.resource.parent_id != container.id:
        folder.resource.parent = container
        folder.resource.save(update_fields=["parent", "updated_at"])
    folder.recompute_path()
    rewrite_descendants(folder)
    return folder


def resolve_folder_path(folder: DocumentFolder, path: str) -> DocumentFolder | None:
    """Find a folder by a path *relative to the folder's own scope*.

    Drag-and-drop sends a path the user sees, which is always inside the same
    workspace/project as the folder being moved, so an empty path means "the
    scope root" and returns None (which the caller reads as "no new parent").
    """
    path = normalize_folder_path(path)
    if not path:
        return None
    node = (
        DocumentFolder.objects.filter(
            workspace=folder.workspace, project=folder.project, path=path
        )
        .select_related("container")
        .first()
    )
    if node is None:
        raise ValidationError({"path": f"Nincs ilyen mappa: {path}"})
    return node


def rewrite_descendants(folder: DocumentFolder) -> None:
    """Recompute every descendant's cached path after the chain moved.

    Documents and files follow their folder, so their cached paths have to be
    recomputed from the chain too - otherwise moving a folder would move the
    folders and leave the documents pointing at paths that no longer exist.
    """
    _repath_contents(folder)
    for child in DocumentFolder.objects.filter(container=folder.resource):
        child.recompute_path()
        rewrite_descendants(child)


def _repath_contents(folder: DocumentFolder) -> None:
    """Give the folder's documents and files the path their folder now implies."""
    from apps.files.models import File

    from .models import Document

    prefix = f"{folder.path}/"
    for model in (Document, File):
        for item in model.objects.filter(folder=folder):
            name = item.path.rsplit("/", 1)[-1]
            wanted = f"{prefix}{name}"
            if item.path == wanted:
                continue
            item.path = wanted
            item.save(update_fields=["path", "updated_at"])


def repath_scope(workspace, project) -> None:
    """Rebuild every cached path in a scope from the container chain.

    The safety net after a bulk change: the chain is the source of truth, so
    recomputing everything makes the cache correct regardless of what moved.
    """
    for folder in DocumentFolder.objects.filter(workspace=workspace, project=project):
        if folder.container_id is None:
            continue
        folder.recompute_path()
        _repath_contents(folder)


def _move_disk(workspace, project, old_path, folder, new_parent=None) -> None:
    """Relocate the folder and everything below it on disk."""
    storage = get_storage()
    new_path = folder.path
    if new_parent is not None:
        new_path = f"{new_parent.path}/{folder.name}" if new_parent.path else folder.name

    if folder.path == new_path:
        return

    for kind in ("documents", "files"):
        base = (
            storage.path_for(
                workspace_id=workspace.pk,
                project_id=project.pk if project else None,
                kind=kind,
                rel_path="",
            )
            / old_path
        )
        if not base.exists():
            continue
        destination = (
            storage.path_for(
                workspace_id=workspace.pk,
                project_id=project.pk if project else None,
                kind=kind,
                rel_path="",
            )
            / new_path
        )
        destination.parent.mkdir(parents=True, exist_ok=True)
        if destination.exists():
            _merge_tree(base, destination)
            continue
        base.replace(destination)
        _prune_empty(base)


def _merge_tree(source, destination) -> None:
    """Move everything from ``source`` into an existing ``destination``."""
    for item in source.iterdir():
        target = destination / item.name
        if item.is_dir():
            target.mkdir(parents=True, exist_ok=True)
            _merge_tree(item, target)
        else:
            item.replace(target)
    _prune_empty(source)


def _prune_empty(directory) -> None:
    try:
        directory.rmdir()
    except OSError:
        pass


def _disk_missing(*, workspace, project, path: str, kind: str = "documents") -> None:
    storage = get_storage()
    target = storage.path_for(
        workspace_id=workspace.pk,
        project_id=project.pk if project else None,
        kind=kind,
        rel_path=path,
    )
    if target.is_dir():
        try:
            target.rmdir()
        except OSError:
            pass


@transaction.atomic
def delete_folder(*, folder: DocumentFolder, move_to_root: bool = False,
                  recursive: bool = False) -> None:
    """Delete a folder.

    Three outcomes, and the middle one is the default on purpose:

    * ``move_to_root=True`` - the contents move up a level and the folder goes.
    * ``recursive=True`` - the whole subtree goes, requested explicitly.
    * neither - **refused** when the folder still holds documents, files or
      subfolders. Deleting a Resource cascades, so the old implicit behaviour
      destroyed an entire subtree on one click; refusing turns that from data loss
      into a prompt.
    """
    from .models import Document

    if move_to_root:
        parent = None
        if folder.container is not None:
            parent = (
                DocumentFolder.objects.filter(resource=folder.container)
                .select_related("container")
                .first()
            )
        for document in Document.objects.filter(folder=folder):
            _move_one(document, folder, parent)
        _move_files_out(folder, parent)
        for child in DocumentFolder.objects.filter(container=folder.resource):
            delete_folder(folder=child, move_to_root=True)
    else:
        has_contents = (
            Document.objects.filter(folder=folder).exists()
            or _files_in(folder).exists()
            or DocumentFolder.objects.filter(container=folder.resource).exists()
        )
        if has_contents and not recursive:
            raise ValidationError(
                {
                    "path": "A mappa nem üres. Mozgasd a tartalmát, töröld a "
                    "mappát almappákkal együtt, vagy jelöld be a rekurzív törlést."
                }
            )
        if recursive:
            for child in DocumentFolder.objects.filter(container=folder.resource):
                delete_folder(folder=child, recursive=True)

    for kind in ("documents", "files"):
        _disk_missing(
            workspace=folder.workspace,
            project=folder.project,
            path=folder.path,
            kind=kind,
        )
    # Deleting the Resource cascades the folder and every subfolder below it.
    Resource.objects.filter(pk=folder.resource_id).delete()


def _files_in(folder: DocumentFolder):
    from apps.files.models import File

    return File.objects.filter(folder=folder)


def _move_files_out(folder: DocumentFolder, new_parent: DocumentFolder | None) -> None:
    """Move the folder's attached files up a level with its documents."""
    from apps.files.models import File

    for stored in File.objects.filter(folder=folder):
        _move_one(stored, folder, new_parent)


def _reparent_resource(item, old_folder: DocumentFolder, new_parent) -> None:
    """Move an item's Resource off the folder it is being moved out of.

    Not optional: a Resource is deleted with its parent, so a document that is
    moved out of a folder but left hanging under that folder's Resource gets
    **deleted with it** the moment the folder is removed. This is also what
    decides the ACL it inherits afterwards.
    """
    container = (
        new_parent.resource
        if new_parent is not None
        else (
            item.project.resource if item.project_id else item.workspace.resource
        )
    )
    resource = item.resource
    if container is None or resource.parent_id == container.id:
        return
    resource.parent = container
    resource.save(update_fields=["parent", "updated_at"])


def _move_one(item, folder: DocumentFolder, new_parent: DocumentFolder | None) -> None:
    """Move a document or file out of ``folder`` into ``new_parent``.

    The path is rewritten relative to the scope, with the new parent's prefix -
    no project name, because a path never carried one.
    """
    prefix = folder.path + "/"
    target = item.path[len(prefix):] if item.path.startswith(prefix) else item.path
    parent_prefix = f"{new_parent.path}/" if new_parent and new_parent.path else ""
    item.path = f"{parent_prefix}{target}"
    item.folder = new_parent
    item.save(update_fields=["path", "folder", "updated_at"])
    _reparent_resource(item, folder, new_parent)

    storage = get_storage()
    kind = "documents" if hasattr(item, "frontmatter") else "files"
    source = storage.path_for(
        workspace_id=item.workspace_id,
        project_id=item.project_id,
        kind=kind,
        rel_path=folder.path + "/" + target,
    )
    if source.exists():
        destination = storage.path_for(
            workspace_id=item.workspace_id,
            project_id=item.project_id,
            kind=kind,
            rel_path=item.path,
        )
        destination.parent.mkdir(parents=True, exist_ok=True)
        source.replace(destination)


def resource_for_path(workspace, project, folder_path: str) -> Resource:
    """The Resource a path lives under, creating the folder (and ancestors).

    Kept as a module function because :class:`~apps.documents.services.DocumentService`
    calls it on every create to pick the document's parent.
    """
    folder_path = normalize_folder_path(folder_path)
    if not folder_path:
        return scope_resource(workspace, project)
    return ensure_folder(workspace, project, folder_path).resource


def register_parents(workspace, project, document_path: str) -> None:
    """Auto-create any missing parent folders for a document path (idempotent)."""
    parts = (document_path or "").split("/")[:-1]
    for index in range(1, len(parts) + 1):
        ensure_folder(workspace, project, "/".join(parts[:index]))


def folder_for_path(workspace, project, path: str) -> DocumentFolder | None:
    """The folder node at ``path`` (without its scope prefix), or None."""
    folder_path = normalize_folder_path(path)
    if not folder_path:
        return None
    return (
        DocumentFolder.objects.filter(workspace=workspace, project=project, path=folder_path)
        .select_related("container")
        .first()
    )


def reparent_document(document) -> None:
    """Point a document's Resource at the folder it now lives in.

    Without this a document keeps the permissions of the place it was *created*,
    so moving it into a tightly-scoped folder would not restrict it, and moving it
    out of one would not free it.
    """
    folder = None
    raw = (document.path or "")
    if "/" in raw:
        # "a/b/c.md" lives in the folder "a/b" - the *folder's* path, not the
        # document's, or the lookup silently misses and the document is left
        # parentless (and therefore with no folder to inherit an ACL from).
        folder = folder_for_path(
            document.workspace, document.project, raw.rsplit("/", 1)[0]
        )
    if document.folder_id == (folder.pk if folder else None):
        return
    document.folder = folder
    document.save(update_fields=["folder", "updated_at"])
    if folder is None:
        parent = (
            document.project.resource if document.project_id else document.workspace.resource
        )
    else:
        parent = folder.resource
    if parent is not None and document.resource.parent_id != parent.id:
        document.resource.parent = parent
        document.resource.save(update_fields=["parent", "updated_at"])


__all__ = [
    "DocumentFolder",
    "ILLEGAL_SEGMENTS",
    "ancestor_paths",
    "create_folder",
    "create_folder_node",
    "delete_folder",
    "ensure_folder",
    "folder_for_path",
    "move_folder",
    "normalize_folder_path",
    "register_parents",
    "rename_folder",
    "repath_scope",
    "reparent_document",
    "resource_for_path",
    "rewrite_descendants",
    "scope_resource",
]