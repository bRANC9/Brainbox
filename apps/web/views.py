"""Knowledge-centric web UI. Every view goes through the permission engine."""

from __future__ import annotations

import difflib
from pathlib import Path

import markdown as md
from django.contrib import messages
from django.contrib.auth import get_user_model
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied, ValidationError
from django.db.models import Count, Q
from django.http import FileResponse, Http404, HttpResponse, HttpResponseForbidden
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils.http import content_disposition_header
from django.views.decorators.http import require_http_methods

from apps.accounts.models import ApiKey
from apps.accounts.services import ApiKeyService
from apps.audit.models import AuditAction, AuditEvent, AuditSource
from apps.audit.services import AuditService
from apps.documents import embeds
from apps.documents.frontmatter import parse_frontmatter
from apps.documents.models import (
    ChangeSource,
    Document,
    DocumentFolder,
    DocumentStatus,
    DocumentVersion,
)
from apps.documents.services import DocumentService
from apps.git.services import GitService
from apps.groups.models import Group, GroupMembership
from apps.knowledge.services import DiscoveryService, DraftService, GraphService
from apps.permissions.constants import Effect, Permission, SubjectType
from apps.permissions.services import PermissionService
from apps.resources.models import Resource, ResourceType
from apps.resources.storage import StorageError
from apps.search.services import SearchService
from apps.workspaces.models import Project, Workspace
from apps.workspaces.ownership import OwnershipService
from apps.workspaces.personal import PersonalWorkspaceService
from apps.workspaces.services import WorkspaceService

User = get_user_model()

MARKDOWN_EXTENSIONS = ["fenced_code", "tables", "toc", "sane_lists", "codehilite", "nl2br"]


def _can(user, resource, permission: str) -> bool:
    return PermissionService.check(user, resource, permission)


def _publish_titles(workspace) -> bool:
    """Whether this workspace allows titles to be published to non-readers.

    Stored on the workspace Resource so a whole area's policy is decided once,
    instead of every document having to opt in individually (and one of them
    being wrong either way).
    """
    return ((workspace.resource.metadata or {}).get("publish_titles")) is True


def _can_publish_titles(user, workspace) -> bool:
    """Only the owner decides who may learn that a document exists.

    Not offered on a Personal workspace: it has exactly one holder, so there is
    no one to publish a title to and the switch would be a no-op.
    """
    if workspace is None or workspace.is_personal:
        return False
    return PermissionService.is_scope_owner(user, workspace.resource)


def _workspace_queryset_with_counts():
    """Workspaces with the card counters the dashboard template renders."""
    return (
        Workspace.objects.select_related("resource")
        .annotate(
            project_count=Count("project_set", distinct=True),
            document_count=Count("documents", distinct=True),
        )
        .order_by("name")
    )


def _render_markdown(content: str, document=None) -> str:
    """Markdown to HTML, with relative references resolved against ``document``.

    ``document`` is optional, so every other caller (a preview, a template with
    no document in hand) keeps working exactly as before - it just gets the
    rendered HTML with its relative URLs untouched. With it, ``![x](photo.png)``
    and ``[n](other.md)`` are rewritten to the routes that serve them, which is
    the only reason a diagram in a document is not a broken image.
    """
    _frontmatter, body = parse_frontmatter(content)
    html = md.markdown(body, extensions=MARKDOWN_EXTENSIONS)
    return embeds.resolve(document, html)


def _git_panel(workspace, project=None) -> dict:
    repository = GitService.repository_for(workspace=workspace, project=project)
    if repository is None:
        return {"git_repository": None}
    return {
        "git_repository": repository,
        "git_state": getattr(repository, "sync_state", None),
        "git_commits": repository.commits.all()[:5],
    }


@login_required
def dashboard(request):
    workspaces = [
        workspace
        for workspace in _workspace_queryset_with_counts()
        if _can(request.user, workspace.resource, Permission.READ)
    ]
    candidates = Document.objects.select_related("resource", "workspace", "project")[:100]
    recent_documents = [
        document
        for document in candidates
        if _can(request.user, document.resource, Permission.READ)
    ][:10]
    return render(
        request,
        "dashboard.html",
        {
            "workspaces": workspaces,
            "recent_documents": recent_documents,
            "personal_workspace": PersonalWorkspaceService.get_for(request.user),
        },
    )


def _folder_tree_rows(workspace, project=None, user=None, tag_filter: str = "") -> list[dict]:
    """Flatten the folder+document tree into rows the template can loop.

    A folder is listed when the caller can read it **or** when it lies on the
    path to something the caller can read: granting access to ``A/B/C`` has to
    leave ``A`` and ``A/B`` visible, otherwise the grant is unusable. Those
    intermediate nodes come back with ``can_read=False`` so the template can
    hide their action buttons - the name is unavoidable (it is part of the
    path), but the sibling folders and the documents next to them are not shown.

    Folders come from the stored DocumentFolder rows plus every parent path of
    the readable documents (so git-imported trees show up too). Rows are
    ``{"type": "dir"|"doc", "name", "depth", ...}``.

    Deliberately not merged with :func:`_node_tree_rows`, even though the walks
    look alike: this one answers "what are the *own* documents of this
    workspace/project scope" and therefore cannot use the other's resource-parent
    walk. The workspace root is not a folder node and has no ``tree_path``, so
    there is no node to hand to the other function; this one keys off the
    ``workspace``/``project`` FKs instead. It also never lists attachments, while
    the node page does, and it has to synthesize ancestor folders for documents
    whose parent row is missing (a git-imported path) - the node page iterates
    real ``DocumentFolder`` rows only. A merge would change at least the
    workspace page (attachments appearing, links changing) without any caller
    asking for it.
    """
    from apps.tags.services import filter_documents_by_tag, tags_for

    documents = [
        doc
        for doc in Document.objects.filter(workspace=workspace, project=project)
        if _can(user, doc.resource, Permission.READ)
    ]
    if tag_filter:
        documents = filter_documents_by_tag(documents, tag_filter)

    scope_root = (
        project.resource if project is not None else workspace.resource
    )
    if project is None:
        # Every folder in the workspace, not only the workspace-root ones: a
        # readable node deep inside a project has to light up its *ancestors*,
        # and that only works if the deeper nodes are in the input to
        # ``visible_resource_ids``. Without them a user whose only grant is a
        # deep folder got an empty tree here and could only reach the content by
        # direct URL or search.
        folders = list(
            DocumentFolder.objects.filter(workspace=workspace).select_related("resource")
        )
    else:
        folders = list(
            DocumentFolder.objects.filter(workspace=workspace, project=project).select_related(
                "resource"
            )
        )
    # Only the nodes whose container *is* this scope are drawn here; the rest
    # were just visibility input.
    scope_folders = [f for f in folders if f.container_id == scope_root.id]
    # Every folder in the scope, at any depth, so a nested row can be drawn and
    # know its own access; ``scope_folders`` are the ones that *start* a row here.
    folder_by_path = {folder.path: folder for folder in folders}
    resource_ids = [folder.resource_id for folder in folders if folder.resource_id]
    resource_ids += [document.resource_id for document in documents]
    visible = set(
        PermissionService.visible_resource_ids(user, resource_ids, Permission.READ)
    )
    readable = set(
        PermissionService.allowed_resource_ids(user, resource_ids, Permission.READ)
    )

    folder_paths = set()
    readable_paths = set()
    for folder in folders:
        if not folder.resource_id:
            continue
        # Readability is a property of the node itself, at any depth: a nested
        # row is rendered in the trie and must report its real access, not the
        # scope's.
        if folder.resource_id in readable:
            readable_paths.add(folder.path)
        # Only the nodes hanging directly off this scope start a row here; deeper
        # ones enter the trie as ancestors of the documents inside them.
        if folder in scope_folders and folder.resource_id in visible:
            folder_paths.add(folder.path)
    for document in documents:
        # A readable document's ancestors have to appear so the tree shows the way
        # to it - but they are *not* readable themselves. Marking them readable
        # would strip the "nincs hozzáférésed" marker off every structural node
        # that happens to sit above something the caller can read.
        parts = (document.path or "").split("/")[:-1]
        for index in range(1, len(parts) + 1):
            folder_paths.add("/".join(parts[:index]))

    # node[path] = {"children": {name: path}, "docs": [...]}
    root = {"children": {}, "docs": []}
    for folder_path in sorted(folder_paths):
        node = root
        prefix = ""
        for segment in folder_path.split("/"):
            prefix = f"{prefix}/{segment}" if prefix else segment
            node["children"].setdefault(segment, {"children": {}, "docs": [], "path": prefix})
            node = node["children"][segment]

    for document in documents:
        parts = (document.path or "").split("/")[:-1]
        node = root
        prefix = ""
        for segment in parts:
            prefix = f"{prefix}/{segment}" if prefix else segment
            node = node["children"][segment]
        node["docs"].append(document)

    rows: list[dict] = []

    def container_href(folder) -> str:
        """Where a structural node should link: its own page.

        Every node has one now - a project, a folder, three levels of both - so a
        trail link is exact instead of pointing at the nearest project and
        stopping there.
        """
        if folder is None:
            return ""
        return _folder_href(folder)

    def walk(node: dict, depth: int, parent_path: str) -> None:
        for name in sorted(node["children"]):
            child = node["children"][name]
            path = f"{parent_path}/{name}" if parent_path else name
            rows.append(
                {
                    "type": "dir",
                    "name": name,
                    "depth": depth,
                    "path": path,
                    # The ops endpoints address by tree path; the create links and
                    # the display keep the scope-relative path, so a row carries
                    # both. In a project scope the tree path is prefixed with the
                    # project node's name.
                    "tree_path": f"{project.name}/{path}" if project is not None else path,
                    "can_read": path in readable_paths,
                    "folder": folder_by_path.get(path),
                    "node_href": (
                        _folder_href(folder_by_path[path])
                        if path in readable_paths and path in folder_by_path
                        else ""
                    ),
                    "trail_href": "" if path in readable_paths
                    else (container_href(folder_by_path[path]) if path in folder_by_path else ""),
                    "tags": _folder_tags(workspace, project, path),
                }
            )
            walk(child, depth + 1, path)
        for document in sorted(node["docs"], key=lambda item: item.path):
            rows.append(
                {
                    "type": "doc",
                    "name": document.title,
                    "depth": depth,
                    "doc": document,
                    "tags": tags_for(document),
                }
            )

    walk(root, 0, "")
    return rows


def _folder_tags(workspace, project, path: str) -> list[str]:
    from apps.documents.models import DocumentFolder
    from apps.tags.services import tags_for

    folder = DocumentFolder.objects.filter(workspace=workspace, project=project, path=path).first()
    return tags_for(folder) if folder else []


@login_required
@require_http_methods(["POST"])
def tree_folder_op(request):
    """Right-click menu operations on a folder: rename / delete, by tree path."""
    import json as _json

    from django.core.exceptions import ValidationError
    from django.http import JsonResponse

    from apps.documents.folders import delete_folder, ensure_node_by_tree_path, rename_folder

    try:
        payload = _json.loads(request.body.decode("utf-8") or "{}")
    except ValueError:
        return JsonResponse({"ok": False, "error": "rossz JSON"}, status=400)

    workspace = get_object_or_404(Workspace, slug=payload.get("workspace"))
    tree_path = (payload.get("node") or "").strip("/")
    if not tree_path:
        return JsonResponse({"ok": False, "error": "nincs mappa megadva"}, status=400)

    # Get-or-create: a row that exists only as a prefix (a git-imported path with
    # no folder row) still gets a node to operate on. Its resource inherits the
    # scope's ACL, so the write check below covers creating it too.
    folder = ensure_node_by_tree_path(workspace, tree_path, created_by=request.user)
    if not _can(request.user, folder.resource, Permission.WRITE):
        return JsonResponse({"ok": False, "error": "Nincs írási jogosultságod."}, status=403)

    op = payload.get("op")
    try:
        if op == "rename":
            name = (payload.get("name") or "").strip("/")
            if not name:
                return JsonResponse({"ok": False, "error": "Üres név."}, status=400)
            # rename_folder only reads the last segment; the parent comes from the
            # chain now, so a bare name is all that is needed.
            rename_folder(folder=folder, path=name.rsplit("/", 1)[-1])
        elif op == "delete":
            delete_folder(folder=folder, move_to_root=payload.get("move") == "up")
        else:
            return JsonResponse({"ok": False, "error": "ismeretlen művelet"}, status=400)
    except ValidationError as exc:
        return JsonResponse({"ok": False, "error": "; ".join(exc.messages)}, status=400)
    return JsonResponse({"ok": True})


@login_required
@require_http_methods(["POST"])
def tree_move(request):
    """Drag-and-drop endpoint: move a document or a folder into a target node.

    ``target`` is a workspace-relative tree path ('' means the scope root), the
    same address the node pages use - not a scope-relative path. The workspace is
    taken from the moved object, so a drag carries only the destination.
    """
    import json as _json

    from django.core.exceptions import ValidationError
    from django.http import JsonResponse

    from apps.documents.folders import DocumentFolder, ensure_node_by_tree_path, move_folder
    from apps.documents.services import DocumentService

    try:
        payload = _json.loads(request.body.decode("utf-8") or "{}")
    except ValueError:
        return JsonResponse({"ok": False, "error": "rossz JSON"}, status=400)

    target = (payload.get("target") or "").strip("/")
    kind = payload.get("type")
    try:
        if kind in {"document", "doc"}:
            document = get_object_or_404(Document, pk=payload.get("id"))
            denied = _require_write_or_403(request, document.resource)
            if denied:
                return denied
            # Get-or-create the destination: a drop onto a folder that exists only
            # as a path prefix still has a node to move into.
            _same_scope_or_error(document.project_id, document.workspace, target)
            destination = ensure_node_by_tree_path(
                document.workspace, target, created_by=request.user
            )
            parent_path = destination.path if destination is not None else ""
            filename = Path(document.path).name
            new_path = f"{parent_path}/{filename}" if parent_path else filename
            DocumentService.move(document, new_path, user=request.user, request=request)
        elif kind == "folder":
            folder = get_object_or_404(DocumentFolder, pk=payload.get("id"))
            # Folders carry their own Resource now, so the check is on the folder
            # and not on the workspace it happens to live in: a write grant on
            # the container is not a write grant on every folder inside it.
            denied = _require_write_or_403(request, folder.resource)
            if denied:
                return denied
            _same_scope_or_error(folder.project_id, folder.workspace, target)
            destination = ensure_node_by_tree_path(
                folder.workspace, target, created_by=request.user
            )
            move_folder(
                folder=folder,
                new_parent=destination,
                # An empty target is the page background: move to the scope root,
                # not "leave it where it is".
                to_root=destination is None,
                user=request.user,
            )
        else:
            return JsonResponse({"ok": False, "error": "ismeretlen típus"}, status=400)
    except ValidationError as exc:
        return JsonResponse({"ok": False, "error": "; ".join(exc.messages)}, status=400)
    return JsonResponse({"ok": True})


def _require_write_or_403(request, resource):
    """Return a 403 response when the caller may not write, else None.

    This used to ``raise HttpResponseForbidden(...)``, which raises TypeError
    (an exception must be a class) and turned a permission failure into a 500.
    Callers must ``return`` the result.
    """
    if not _can(request.user, resource, Permission.WRITE):
        return HttpResponseForbidden("Nincs írási jogosultságod ehhez a mappához.")
    return None


def _safe_next(request) -> str:
    """A same-site path to return to after a tag edit, or ''.

    Only an absolute path is accepted, and a leading ``//`` is rejected too: it
    is a protocol-relative URL and redirecting to it would be an open redirect.
    """
    target = (request.POST.get("next") or "").strip()
    if target.startswith("/") and not target.startswith("//"):
        return target
    return ""


def _project_of_node(node):
    """The Project a node belongs to, or None (delegates to the shared helper)."""
    from apps.documents.folders import project_of_node

    return project_of_node(node)


def _same_scope_or_error(project_id, workspace, tree_path):
    """Raise when a destination tree path is outside the moved object's scope.

    A document or folder lives under its project (or the workspace), and a move
    only rewrites the path *inside* that scope - so a destination in another
    project would compute a path that belongs nowhere. Read from the target's
    first segment and checked *before* any node is created for it, so a refused
    move leaves no stray folder behind. The interface cannot produce a
    cross-scope drag, but the endpoint is public.
    """
    from django.core.exceptions import ValidationError

    from apps.documents.folders import split_tree_path

    project, _ = split_tree_path(workspace, tree_path)
    if (project.pk if project else None) != project_id:
        raise ValidationError({"path": "Csak a saját projektjén belül mozgatható."})


def _node_scope(workspace, tree_path):
    """``(node, project, container)`` for a node path, or the workspace root.

    Every action view used to look a Project up by slug; they all take the same
    tree path the node page uses now, so one helper answers for all of them. An
    empty path is the workspace root, which is not a node in the tree.
    """
    if not tree_path:
        return None, None, workspace.resource
    from apps.documents.folders import folder_by_tree_path

    node = folder_by_tree_path(workspace, tree_path)
    if node is None:
        raise Http404
    return node, _project_of_node(node), node.resource


def _crumb_node(node):
    """A node to show as a breadcrumb step, or None when it *is* the project.

    A project node already renders as the project crumb on every form, so
    returning it here too would print the same name twice.
    """
    if node is None or node.role == "project":
        return None
    return node


def _scope_prefix(node) -> str:
    """A node's path *relative to its storage scope*, for the new/edit forms.

    The scope root is the project node (or the workspace), and a project node's
    own stored ``path`` is its name - the scope's name, which must not be
    repeated *inside* the scope. So a project node contributes no prefix, while a
    folder inside one contributes its already scope-relative ``path``. This is
    what keeps "Új mappa" on a project's own page from creating ``Deploy/Deploy``.
    """
    from apps.documents.models import FolderRole

    if node is None or node.role == FolderRole.PROJECT:
        return ""
    return node.path


def _node_redirect(workspace, node):
    """Back to the node that was acted on, or to the workspace root."""
    if node is None:
        return redirect("web:workspace_detail", workspace_slug=workspace.slug)
    return redirect(
        "web:folder_detail", workspace_slug=workspace.slug, tree_path=node.tree_path()
    )


def _tree_action_urls(workspace, node) -> dict:
    """The action URLs the file-tree panel needs, for this scope or node.

    Computed once per page instead of the panel branching on whether a project
    exists: the same panel renders at the workspace root and on any node, and
    ``_file_tree.html`` just reads these keys.
    """
    if node is None:
        return {
            "doc_new": reverse("web:workspace_document_create", args=[workspace.slug]),
            "folder_new": reverse("web:workspace_folder_create", args=[workspace.slug]),
            "bulk_upload": reverse("web:workspace_bulk_upload", args=[workspace.slug]),
            "files": reverse("web:workspace_files", args=[workspace.slug]),
            "git_pull": reverse("web:workspace_git_pull", args=[workspace.slug]),
        }
    path = node.tree_path()
    return {
        "doc_new": reverse("web:node_document_create", args=[workspace.slug, path]),
        "folder_new": reverse("web:node_folder_create", args=[workspace.slug, path]),
        "bulk_upload": reverse("web:node_bulk_upload", args=[workspace.slug, path]),
        "files": reverse("web:node_files", args=[workspace.slug, path]),
        "git_pull": reverse("web:node_git_pull", args=[workspace.slug, path]),
    }


@login_required
def folder_create(request, workspace_slug, tree_path=None):
    """Create a folder in the workspace root, or inside any node."""
    from django.core.exceptions import ValidationError

    from apps.documents.folders import create_folder

    workspace = get_object_or_404(Workspace, slug=workspace_slug)
    node, project, container = _node_scope(workspace, tree_path)
    if not _can(request.user, container, Permission.WRITE):
        return HttpResponseForbidden("You do not have write access here.")

    if request.method == "POST":
        parent = (request.POST.get("parent") or "").strip("/")
        raw_paths = [
            line.strip()
            for line in (request.POST.get("paths") or request.POST.get("path") or "").splitlines()
            if line.strip()
        ]
        if not raw_paths:
            messages.error(request, "Adj meg legalább egy mappanevet.")
        else:
            ok, failed = 0, []
            for raw in raw_paths:
                folder_target = f"{parent}/{raw}" if parent else raw
                try:
                    create_folder(
                        workspace=workspace,
                        project=project,
                        path=folder_target,
                        created_by=request.user,
                    )
                    ok += 1
                except ValidationError as exc:
                    failed.append(f"{raw}: {'; '.join(exc.messages)}")
            if ok:
                messages.success(request, f"{ok} mappa létrehozva.")
            for item in failed:
                messages.warning(item)
        return _node_redirect(workspace, node)

    return render(
        request,
        "folder_form.html",
        {
            "workspace": workspace,
            "project": project,
            "node": _crumb_node(node),
            "parent": (request.GET.get("parent") or "").strip("/"),
        },
    )


@login_required
def search(request):
    mode_labels = {"hybrid": "hibrid", "text": "szöveges", "semantic": "szemantikus"}
    query = request.GET.get("q", "").strip()
    mode = request.GET.get("mode", "hybrid")
    if mode not in mode_labels:
        mode = "hybrid"
    results = []
    if query:
        results = SearchService.search(request.user, query, mode=mode, limit=25)
        AuditService.log(
            AuditAction.SEARCH,
            user=request.user,
            source=AuditSource.WEB,
            request=request,
            detail={"q": query, "mode": mode, "count": len(results)},
        )
        # The service returns workspace/project ids; the UI answers "where does
        # this live?", so resolve the names the dashboard shows for a document.
        workspace_ids = {row["workspace"] for row in results if row["workspace"]}
        project_ids = {row["project"] for row in results if row["project"]}
        workspace_names = {
            str(pk): name
            for pk, name in Workspace.objects.filter(pk__in=workspace_ids).values_list(
                "pk", "name"
            )
        }
        project_names = {
            str(pk): name
            for pk, name in Project.objects.filter(pk__in=project_ids).values_list("pk", "name")
        }
        results = [
            {
                **row,
                "workspace_name": workspace_names.get(row["workspace"], ""),
                "project_name": project_names.get(row["project"], ""),
            }
            for row in results
        ]
    return render(
        request,
        "search.html",
        {
            "query": query,
            "mode": mode,
            "mode_labels": mode_labels,
            "mode_label": mode_labels[mode],
            "results": results,
        },
    )


def _entry_rows(resource) -> list[dict]:
    """ACL entries with a human label, ordered for display."""
    rows = []
    for entry in resource.acl_entries.all().select_related("created_by"):
        if entry.subject_type == SubjectType.GROUP:
            group = Group.objects.filter(pk=entry.subject_id).first()
            label = group.name if group else "(törölt csoport)"
        else:
            subject = User.objects.filter(pk=entry.subject_id).first()
            label = subject.username if subject else "(törölt user)"
        rows.append({"entry": entry, "label": label})
    return rows


def _access_context(request, resource) -> dict:
    """Context for the shared access (ACL) panel, or {} when not permitted.

    The keys must stay unprefixed: `_access_panel.html` is included both here
    and from the standalone permissions view, which supplies the same names.
    """
    can_manage = PermissionService.can_manage_acl(request.user, resource)
    # `can_share` means "may create an ACL entry at some level", which is why the
    # owner has it too - they share through `can_manage`, not instead of it.
    can_share = _can(request.user, resource, Permission.WRITE)
    personal = PermissionService.is_personal(resource)
    if not (can_manage or can_share):
        return {}
    if personal:
        # A Personal workspace has exactly one holder and is not shareable, so
        # the panel must not hint that either is possible. The engine refuses the
        # write anyway; the UI just has to agree with it.
        return {
            "entry_rows": _entry_rows(resource),
            "can_admin": False,
            "can_share": False,
            "is_owner": PermissionService.is_scope_owner(request.user, resource),
            "is_personal": True,
            "is_workspace_resource": resource.resource_type == ResourceType.WORKSPACE,
            "no_takeover": resource.takeover_locked(),
            "can_take_over": False,
            "access_url": reverse("web:resource_permissions", args=[resource.pk]),
            "search": None,
        }
    workspace = PermissionService.workspace_of(resource)
    return {
        "entry_rows": _entry_rows(resource),
        "can_admin": can_manage,
        "can_share": can_share,
        "is_owner": PermissionService.is_scope_owner(request.user, resource),
        "is_personal": False,
        "is_workspace_resource": resource.resource_type == ResourceType.WORKSPACE,
        "no_takeover": resource.takeover_locked(),
        "can_take_over": PermissionService.can_take_over(request.user, resource),
        "publish_titles": _publish_titles(workspace) if workspace else False,
        "can_publish_titles": (
            _can_publish_titles(request.user, workspace) if workspace else False
        ),
        "access_url": reverse("web:resource_permissions", args=[resource.pk]),
        "search": _subject_matches(request.user, resource, request.GET.get("q", "")),
    }


@login_required
def workspace_detail(request, workspace_slug):
    workspace = get_object_or_404(Workspace, slug=workspace_slug)
    # can_browse, not check: somebody granted a deep folder inside this
    # workspace has to be able to open it, or the grant is unusable.
    if not PermissionService.can_browse(request.user, workspace.resource):
        raise Http404

    documents = [
        document
        for document in workspace.documents.filter(project__isnull=True).select_related("resource")
        if _can(request.user, document.resource, Permission.READ)
    ]
    can_write = _can(request.user, workspace.resource, Permission.WRITE)
    tag_filter = request.GET.get("tag", "")
    tree_rows = _folder_tree_rows(workspace, None, request.user, tag_filter)
    context = {
        "workspace": workspace,
        "documents": documents,
        "can_write": can_write,
        "is_personal": workspace.is_personal,
        "owner": workspace.owner,
        "can_rename": _can(request.user, workspace.resource, Permission.ADMIN),
        **_access_context(request, workspace.resource),
        "tree_rows": tree_rows,
        "tag_filter": tag_filter,
        "available_tags": _available_tags(workspace, None),
        **_git_panel(workspace),
        "tree_urls": _tree_action_urls(workspace, None),
        "tree_node_path": "",
    }
    return render(request, "workspace_detail.html", context)


def _subtree_folders(node, max_depth: int = 16) -> list:
    """The node and every folder below it, breadth-first, depth-bounded."""
    from apps.documents.models import DocumentFolder

    out = [node]
    frontier = [node.resource_id]
    for _ in range(max_depth):
        children = list(
            DocumentFolder.objects.filter(container_id__in=frontier).select_related("resource")
        )
        if not children:
            break
        out.extend(children)
        frontier = [child.resource_id for child in children]
    return out


def _node_tree_rows(workspace, node, user, tag_filter: str = "") -> list[dict]:
    """The node's whole subtree, flattened, documents and attachments together.

    Flattened rather than one level, because the request was to keep a folder page
    as informative as a project page was: you see everything below the node in one
    list, indented by depth, and click *in* through the folder names (each is a
    page of its own).

    The walk descends into any folder the caller can *see* - not only the ones
    they can read - because a grant sits under a trail of folders they cannot
    read; that trail is the way down. Everything else in those folders stays
    hidden: it is not in the visible set.

    Deliberately not merged with :func:`_folder_tree_rows`: this one walks a
    node's *subtree* over ``Resource.parent`` and lists attachments, while the
    scope variant lists a workspace/project's own documents and has no folder
    node to start from at the workspace root. See that function's docstring for
    the full list of differences.
    """
    from apps.documents.models import Document
    from apps.files.models import File
    from apps.tags.services import filter_documents_by_tag, tags_for

    subtree = _subtree_folders(node)
    subtree_ids = [f.resource_id for f in subtree]
    children_of: dict = {}
    for folder in subtree:
        children_of.setdefault(folder.container_id, []).append(folder)

    documents = [
        document
        for document in Document.objects.filter(resource__parent_id__in=subtree_ids).select_related(
            "resource"
        )
        if _can(user, document.resource, Permission.READ)
    ]
    if tag_filter:
        documents = filter_documents_by_tag(documents, tag_filter)
    attachments = [
        stored
        for stored in File.objects.filter(resource__parent_id__in=subtree_ids).select_related(
            "resource"
        )
        if _can(user, stored.resource, Permission.READ)
    ]

    resource_ids = subtree_ids + [item.resource_id for item in (*documents, *attachments)]
    visible = set(PermissionService.visible_resource_ids(user, resource_ids, Permission.READ))
    readable = set(PermissionService.allowed_resource_ids(user, resource_ids, Permission.READ))

    docs_of: dict = {}
    for document in documents:
        docs_of.setdefault(document.resource.parent_id, []).append(document)
    files_of: dict = {}
    for stored in attachments:
        files_of.setdefault(stored.resource.parent_id, []).append(stored)

    rows: list[dict] = []

    def walk(folder, depth: int) -> None:
        for child in sorted(
            children_of.get(folder.resource_id, []), key=lambda item: item.name.lower()
        ):
            if child.resource_id not in visible:
                continue
            can_read = child.resource_id in readable
            rows.append(
                {
                    "type": "dir",
                    "name": child.name,
                    "depth": depth,
                    "path": child.path,
                    "tree_path": child.tree_path(),
                    "can_read": can_read,
                    "folder": child,
                    "node_href": _folder_href(child),
                    "trail_href": "" if can_read else _folder_href(child),
                    "tags": tags_for(child),
                }
            )
            # Descend whenever the folder is visible: a trail node is the way down
            # to what was granted, even though it is not readable itself.
            walk(child, depth + 1)

        for document in sorted(docs_of.get(folder.resource_id, []), key=lambda item: item.path):
            rows.append(
                {
                    "type": "doc",
                    "name": document.title,
                    "depth": depth,
                    "doc": document,
                    "tags": tags_for(document),
                }
            )
        for stored in sorted(files_of.get(folder.resource_id, []), key=lambda item: item.path):
            rows.append(
                {
                    "type": "file",
                    "name": stored.name,
                    "depth": depth,
                    "file": stored,
                    "size": _human_size(stored.size),
                }
            )

    walk(node, 0)
    return rows


def _folder_href(folder) -> str:
    """The canonical address of a node: its tree path under its workspace."""
    return reverse("web:folder_detail", args=[folder.workspace.slug, folder.tree_path()])


@login_required
def folder_detail(request, workspace_slug, tree_path):
    """A page for any node in the tree, addressed by its workspace-relative path.

    This is what replaces "a project is a slug in a URL" with "a node is a path":
    ``/workspaces/ecoform/f/Deploy/runbooks/`` addresses the same object whether it
    is a project, a folder or three levels of both - because after the tree
    change they are one kind of thing.
    """
    from apps.documents.folders import folder_by_tree_path

    workspace = get_object_or_404(Workspace, slug=workspace_slug)
    node = folder_by_tree_path(workspace, tree_path)
    if node is None:
        raise Http404
    if not PermissionService.can_browse(request.user, node.resource):
        raise Http404
    # A project node's own `project` FK is NULL (it *is* the project), so the
    # Project has to be looked up through the shared resource - the templates and
    # the tree JS need the slug to scope their operations.
    project = _project_of_node(node)

    tag_filter = request.GET.get("tag", "")
    ancestors = [
        {"name": parent.name, "href": _folder_href(parent)}
        for parent in reversed(node.chain()[1:])
    ]
    context = {
        "workspace": workspace,
        "project": project,
        "folder_node": node,
        "folder_prefix": _scope_prefix(node),
        "ancestors": ancestors,
        "can_write": _can(request.user, node.resource, Permission.WRITE),
        "owner": node.owner,
        **_access_context(request, node.resource),
        "tree_rows": _node_tree_rows(workspace, node, request.user, tag_filter),
        "tag_filter": tag_filter,
        "available_tags": _available_tags(workspace, project),
        **_git_panel(workspace, project),
        "tree_urls": _tree_action_urls(workspace, node),
        "tree_node_path": node.tree_path(),
    }
    return render(request, "folder_detail.html", context)


@login_required
def document_detail(request, pk):
    document = get_object_or_404(Document.objects.select_related("workspace", "project"), pk=pk)
    if not _can(request.user, document.resource, Permission.READ):
        raise Http404

    content = DocumentService.read_content(document)
    from apps.tags.services import effective_tags

    outgoing = [
        link
        for link in document.resource.outgoing_links.select_related("target")
        if _can(request.user, link.target, Permission.READ)
    ]
    incoming = [
        link
        for link in document.resource.incoming_links.select_related("source")
        if _can(request.user, link.source, Permission.READ)
    ]
    return render(
        request,
        "document_detail.html",
        {
            "document": document,
            "content": content,
            "rendered_html": _render_markdown(content, document),
            "outgoing_links": outgoing,
            "incoming_links": incoming,
            "related": GraphService.neighbors(request.user, document.resource, depth=1),
            "tags": effective_tags(document),
            "can_write": _can(request.user, document.resource, Permission.WRITE),
            "can_admin": _can(request.user, document.resource, Permission.ADMIN),
            "summary_published": (document.resource.metadata or {}).get("public_summary")
            is True,
            "publish_titles": _publish_titles(document.workspace),
        },
    )


@login_required
@require_http_methods(["GET", "POST"])
def document_bulk_upload(request, workspace_slug, tree_path=None):
    """Upload many .md files into a folder of the workspace root or any node."""
    from apps.documents.folders import ensure_folder
    from apps.documents.services import DocumentService

    workspace = get_object_or_404(Workspace, slug=workspace_slug)
    node, project, container = _node_scope(workspace, tree_path)
    if not _can(request.user, container, Permission.WRITE):
        return HttpResponseForbidden("You do not have write access here.")

    if request.method == "POST":
        folder = (request.POST.get("folder") or "").strip()
        uploads = [
            (uploaded.name, uploaded.read()) for uploaded in request.FILES.getlist("files")
        ]
        if not uploads:
            messages.error(request, "Válassz ki legalább egy fájlt.")
        else:
            if folder:
                ensure_folder(workspace, project, folder, created_by=request.user)
            result = DocumentService.bulk_create_from_files(
                workspace=workspace,
                project=project,
                folder=folder,
                uploads=uploads,
                created_by=request.user,
                source=ChangeSource.WEB,
                request=request,
            )
            messages.success(request, f"{len(result['created'])} fájl betöltve.")
            for skipped in result["skipped"]:
                messages.warning(f"{skipped['file']}: {skipped['reason']}")
        return _node_redirect(workspace, node)

    folders = [
        row["path"]
        for row in _folder_tree_rows(workspace, project, request.user)
        if row["type"] == "dir"
    ]
    return render(
        request,
        "document_bulk_form.html",
        {"workspace": workspace, "project": project, "node": _crumb_node(node), "folders": folders},
    )


@login_required
@require_http_methods(["GET", "POST"])
def document_create(request, workspace_slug, tree_path=None):
    workspace = get_object_or_404(Workspace, slug=workspace_slug)
    node, project, container = _node_scope(workspace, tree_path)
    if not _can(request.user, container, Permission.WRITE):
        return HttpResponseForbidden("You do not have write access here.")

    if request.method == "POST":
        from apps.documents.folders import ensure_folder

        path = (request.POST.get("path") or "").strip()
        if path:
            parent = path.rsplit("/", 1)[0] if "/" in path else ""
            if parent:
                ensure_folder(workspace, project, parent, created_by=request.user)
        document = DocumentService.create(
            workspace=workspace,
            project=project,
            title=request.POST.get("title", ""),
            path=path or None,
            content=request.POST.get("content", ""),
            summary=request.POST.get("summary", ""),
            status=request.POST.get("status") or None,
            is_template=request.POST.get("is_template") == "on",
            created_by=request.user,
            source=ChangeSource.WEB,
            request=request,
        )
        if request.POST.get("publish_summary") == "on" and _publish_titles(workspace):
            DocumentService.set_published_summary(
                document, True, user=request.user, request=request
            )
        messages.success(request, f"Document '{document.title}' created.")
        return redirect("web:document_detail", pk=document.pk)

    templates = [
        t
        for t in Document.objects.filter(
            workspace=workspace, project=project, is_template=True
        )
        if _can(request.user, t.resource, Permission.READ)
    ]
    prefill = ""
    template_id = request.GET.get("template")
    if template_id:
        chosen = next((t for t in templates if str(t.pk) == str(template_id)), None)
        if chosen is not None:
            from apps.documents.services import DocumentService as _Svc

            prefill = _Svc.read_content(chosen)
    return render(
        request,
        "document_form.html",
        {
            "workspace": workspace,
            "project": project,
            "node": _crumb_node(node),
            "document": None,
            "content": prefill,
            "statuses": DocumentStatus.choices,
            "folders": _folder_tree_rows(workspace, project, request.user),
            "publish_summary": False,
            "publish_titles": _publish_titles(workspace),
            "prefill_path": (request.GET.get("folder", "") + "/")
            if request.GET.get("folder")
            else "",
            "templates": templates,
        },
    )


@login_required
@require_http_methods(["GET", "POST"])
def document_edit(request, pk):
    document = get_object_or_404(Document.objects.select_related("workspace", "project"), pk=pk)
    if not _can(request.user, document.resource, Permission.WRITE):
        return HttpResponseForbidden("You do not have write access to this document.")

    if request.method == "POST":
        new_path = (request.POST.get("path") or "").strip()
        if new_path and new_path != document.path:
            try:
                DocumentService.move(document, new_path, user=request.user, request=request)
                document.refresh_from_db()
                messages.success(request, f"Áthelyezve ide: {document.path}")
            except ValidationError as exc:
                messages.error(request, "; ".join(exc.messages))
                return redirect("web:document_edit", pk=document.pk)
        DocumentService.update_content(
            document=document,
            content=request.POST.get("content", ""),
            title=request.POST.get("title") or None,
            summary=request.POST.get("summary"),
            status=request.POST.get("status") or None,
            priority=int(request.POST["priority"]) if request.POST.get("priority") else None,
            user=request.user,
            source=ChangeSource.WEB,
            request=request,
        )
        if _publish_titles(document.workspace):
            # Only when the workspace actually allows publication. The checkbox
            # is rendered disabled otherwise, and a disabled checkbox is never
            # submitted - so writing `False` here would silently clear the stored
            # flag every time somebody saved a document in a workspace with
            # publication switched off.
            DocumentService.set_published_summary(
                document,
                request.POST.get("publish_summary") == "on",
                user=request.user,
                request=request,
            )
        messages.success(request, "Document saved as a new version.")
        return redirect("web:document_detail", pk=document.pk)

    return render(
        request,
        "document_form.html",
        {
            "workspace": document.workspace,
            "project": document.project,
            "document": document,
            "content": DocumentService.read_content(document),
            "statuses": DocumentStatus.choices,
            "folders": _folder_tree_rows(document.workspace, document.project, request.user),
            "publish_summary": (document.resource.metadata or {}).get("public_summary") is True,
            "publish_titles": _publish_titles(document.workspace),
            "prefill_path": "",
        },
    )


@login_required
def document_history(request, pk):
    document = get_object_or_404(Document.objects.select_related("workspace", "project"), pk=pk)
    if not _can(request.user, document.resource, Permission.READ):
        raise Http404

    versions = list(document.versions.all())
    diff = ""
    from_version = request.GET.get("from")
    to_version = request.GET.get("to")
    if from_version and to_version:
        try:
            source = document.versions.get(version=int(from_version))
            target = document.versions.get(version=int(to_version))
            diff = "\n".join(
                difflib.unified_diff(
                    source.content.splitlines(),
                    target.content.splitlines(),
                    fromfile=f"v{source.version}",
                    tofile=f"v{target.version}",
                    lineterm="",
                )
            )
        except DocumentVersion.DoesNotExist:
            messages.error(request, "Requested version does not exist.")

    return render(
        request,
        "document_history.html",
        {"document": document, "versions": versions, "diff": diff},
    )


@login_required
@require_http_methods(["POST"])
def git_pull(request, workspace_slug, tree_path=None):
    """Git sync a workspace, or a project node inside it.

    One view for both: a project is a node, so ``/f/<path>/git/pull/`` and the
    workspace-root ``/git/pull/`` differ only in which repository they target.
    Non-project nodes fall back to the workspace repository (``project`` is None),
    which is exactly what the old workspace-scoped view did.
    """
    workspace = get_object_or_404(Workspace, slug=workspace_slug)
    node, project, container = _node_scope(workspace, tree_path)
    if not _can(request.user, container, Permission.WRITE):
        return HttpResponseForbidden("You do not have write access here.")
    repository = GitService.repository_for(workspace=workspace, project=project)
    if repository is None:
        messages.error(request, "There is no Git repository here.")
    else:
        try:
            result = GitService.pull_repository(repository, user=request.user, request=request)
            messages.success(request, f"Git sync complete: {result}")
        except Exception as exc:  # noqa: BLE001 - surface the message in the UI
            messages.error(request, f"Git sync failed: {exc}")
    return _node_redirect(workspace, node)


# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------
# Calendar / agenda / deadlines
# ---------------------------------------------------------------------------
def _accessible_deadlines(user, *, workspace_id=None, project_id=None):
    from apps.deadlines.models import DeadlineStatus, KnowledgeDeadline

    deadlines = KnowledgeDeadline.objects.select_related(
        "document", "workspace", "project", "resource"
    )
    if workspace_id:
        deadlines = deadlines.filter(workspace_id=workspace_id)
    if project_id:
        deadlines = deadlines.filter(project_id=project_id)
    return [
        d
        for d in deadlines
        if d.status != DeadlineStatus.DISMISSED and _can(user, d.resource, Permission.READ)
    ]


def _int_param(request, name: str, default: int, *, minimum: int, maximum: int) -> int:
    """Read an int query param, clamped to a range.

    A hand-edited or hostile value must not raise: `?year=abc` used to bubble up
    a ValueError and return a 500.
    """
    raw = request.GET.get(name)
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return default
    return max(minimum, min(maximum, value))


MONTH_NAMES_HU = [
    "",
    "január",
    "február",
    "március",
    "április",
    "május",
    "június",
    "július",
    "augusztus",
    "szeptember",
    "október",
    "november",
    "december",
]


@login_required
def calendar(request):
    """Month view of deadlines."""
    import calendar as calmod
    from datetime import date

    today = date.today()
    year = _int_param(request, "year", today.year, minimum=1970, maximum=9999)
    month = _int_param(request, "month", today.month, minimum=1, maximum=12)
    cal = calmod.Calendar(firstweekday=0)
    month_grid = cal.monthdayscalendar(year, month)

    deadlines = _accessible_deadlines(
        request.user,
        workspace_id=request.GET.get("workspace"),
        project_id=request.GET.get("project"),
    )
    by_day: dict[int, list] = {}
    for deadline in deadlines:
        if deadline.due_date.year == year and deadline.due_date.month == month:
            by_day.setdefault(deadline.due_date.day, []).append(deadline)

    # Month arithmetic via the Calendar class (no module-level helpers).
    if month == 1:
        prev_year, prev_month = year - 1, 12
    else:
        prev_year, prev_month = year, month - 1
    if month == 12:
        next_year, next_month = year + 1, 1
    else:
        next_year, next_month = year, month + 1

    # Flat per-day list the template can loop without dict lookups.
    days = [
        {"num": day, "events": by_day.get(day, [])}
        for day in range(1, calmod.monthrange(year, month)[1] + 1)
    ]
    return render(
        request,
        "calendar.html",
        {
            "year": year,
            "month": month,
            "month_name": MONTH_NAMES_HU[month],
            "month_grid": month_grid,
            "days": days,
            "today": today,
            "prev_year": prev_year,
            "prev_month": prev_month,
            "prev_month_name": MONTH_NAMES_HU[prev_month],
            "next_year": next_year,
            "next_month": next_month,
            "next_month_name": MONTH_NAMES_HU[next_month],
            "weekday_names": ["Hét", "Ked", "Sze", "Csü", "Pén", "Szo", "Vas"],
        },
    )


@login_required
def agenda(request):
    """Agenda view: overdue + upcoming deadlines grouped by day."""
    from datetime import timedelta

    from django.utils import timezone as djtz

    today = djtz.localdate()
    # Clamped: a huge `days` used to raise OverflowError inside timedelta.
    horizon = _int_param(request, "days", 30, minimum=1, maximum=365)
    until = today + timedelta(days=horizon)
    deadlines = [
        d
        for d in _accessible_deadlines(
            request.user,
            workspace_id=request.GET.get("workspace"),
            project_id=request.GET.get("project"),
        )
        if d.due_date <= until
    ]
    overdue = sorted([d for d in deadlines if d.due_date < today], key=lambda d: d.due_date)
    upcoming = sorted([d for d in deadlines if d.due_date >= today], key=lambda d: d.due_date)
    return render(
        request, "agenda.html", {"overdue": overdue, "upcoming": upcoming, "today": today, "days": horizon}
    )


@login_required
def deadlines_ical(request):
    """iCal feed of open deadlines (subscribe from a calendar app)."""
    from datetime import datetime, timedelta

    from django.utils import timezone as djtz

    today = djtz.localdate()
    horizon = _int_param(request, "days", 365, minimum=1, maximum=3650)
    until = today + timedelta(days=horizon)
    rows = [
        d
        for d in _accessible_deadlines(
            request.user,
            workspace_id=request.GET.get("workspace"),
            project_id=request.GET.get("project"),
        )
        if today - timedelta(days=30) <= d.due_date <= until and d.status == "open"
    ]

    def esc(text):
        return (
            str(text or "")
            .replace("\\", "\\\\")
            .replace(";", r"\;")
            .replace(",", r"\,")
            .replace("\n", r"\n")
        )

    stamp = datetime.utcnow().strftime("%Y%m%dT%H%M%SZ")
    lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "PRODID:-//Brainbox//Deadlines//EN",
        "CALSCALE:GREGORIAN",
    ]
    for deadline in rows:
        lines += [
            "BEGIN:VEVENT",
            f"UID:{deadline.id}@brainbox",
            f"DTSTAMP:{stamp}",
            f"DTSTART;VALUE=DATE:{deadline.due_date.strftime('%Y%m%d')}",
            f"SUMMARY:{esc(deadline.title)}",
            f"DESCRIPTION:{esc(deadline.context or deadline.document.title)}",
            f"URL:{request.build_absolute_uri('/documents/%s/' % deadline.document_id)}",
            "END:VEVENT",
        ]
    lines.append("END:VCALENDAR")
    response = HttpResponse("\r\n".join(lines), content_type="text/calendar; charset=utf-8")
    response["Content-Disposition"] = 'attachment; filename="brainbox-deadlines.ics"'
    return response


# Management UI (Phase 6)
# ---------------------------------------------------------------------------
@login_required
def settings_page(request):
    """Runtime settings editor (superuser): AI/embedding, search, git, jobs, monitoring."""
    from django.core.exceptions import ValidationError

    from apps.settings_store.services import clear_override, describe, set_value

    if not request.user.is_superuser:
        return HttpResponseForbidden("Superuser access required.")

    if request.method == "POST":
        key = request.POST.get("key", "")
        if request.POST.get("reset") == "1":
            clear_override(key)
            messages.success(request, f"{key}: env default visszaállítva.")
        else:
            try:
                set_value(key=key, raw=request.POST.get("value", ""), user=request.user)
                messages.success(request, f"{key} elmentve.")
            except ValidationError as exc:
                messages.error(request, "; ".join(exc.messages))
        return redirect("web:settings_page")


    grouped: dict[str, list[dict]] = {}
    for row in describe():
        grouped.setdefault(row["category"], []).append(row)

    return render(request, "settings.html", {"grouped": grouped})


@login_required
def settings_test(request):
    """Run a connectivity probe for one setting (the 'tesztelés' button)."""
    from apps.settings_store.probes import probe_for_key

    if not request.user.is_superuser:
        return HttpResponseForbidden("Superuser access required.")

    key = request.POST.get("key", "")
    deep = request.POST.get("deep") == "1"
    try:
        result = probe_for_key(key, deep=deep)
    except Exception as exc:  # noqa: BLE001 - surface the error in the UI
        result = {"ok": False, "detail": f"{type(exc).__name__}: {exc}"}

    request.session["settings_probe"] = {"key": key, **result}
    return redirect(f"{reverse('web:settings_page')}#probe")


def _available_tags(workspace, project) -> list[str]:
    """Tag vocabulary used in this scope, for the filter bar."""
    from apps.documents.models import Document, DocumentFolder
    from apps.tags.services import tags_for

    names = set()
    for document in Document.objects.filter(workspace=workspace, project=project):
        names.update(tags_for(document))
    for folder in DocumentFolder.objects.filter(workspace=workspace, project=project):
        names.update(tags_for(folder))
    return sorted(n for n in names if n)


@login_required
@require_http_methods(["POST"])
def document_tags(request, pk):
    """Attach/detach tags on a document (from the document page or context menu)."""
    from django.http import JsonResponse

    from apps.documents.models import Document
    from apps.tags.services import tag_target, untag_target

    document = get_object_or_404(Document.objects.select_related("resource"), pk=pk)
    if not _can(request.user, document.resource, Permission.WRITE):
        return HttpResponseForbidden("Nincs írási jogosultságod ehhez a dokumentumhoz.")

    action = request.POST.get("action")
    names = [
        part.strip()
        for part in (request.POST.get("tags") or "").replace(",", " ").split()
        if part.strip()
    ]
    if action == "remove":
        tags = untag_target(document, names)
    elif action == "add":
        tags = tag_target(document, names, user=request.user)
    else:
        return JsonResponse({"ok": False, "error": "Ismeretlen művelet"}, status=400)

    if request.headers.get("X-Requested-With") == "fetch":
        return JsonResponse({"ok": True, "tags": tags})
    next_url = _safe_next(request)
    if next_url:
        return redirect(next_url)
    return redirect("web:document_detail", pk=document.pk)


@login_required
@require_http_methods(["POST"])
def folder_tags(request):
    """Attach/detach tags on a folder (affects its subtree when filtering)."""
    from django.http import JsonResponse

    from apps.documents.folders import ensure_node_by_tree_path
    from apps.tags.services import tag_target, untag_target

    workspace = get_object_or_404(Workspace, slug=request.POST.get("workspace"))
    tree_path = (request.POST.get("node") or "").strip("/")
    if not tree_path:
        return HttpResponseForbidden("Nincs mappa megadva.")

    folder = ensure_node_by_tree_path(workspace, tree_path, created_by=request.user)
    if not _can(request.user, folder.resource, Permission.WRITE):
        return HttpResponseForbidden("Nincs írási jogosultságod ehhez a mappához.")

    names = [
        part.strip()
        for part in (request.POST.get("tags") or "").replace(",", " ").split()
        if part.strip()
    ]
    action = request.POST.get("action")
    tags = untag_target(folder, names) if action == "remove" else tag_target(folder, names, user=request.user)

    if request.headers.get("X-Requested-With") == "fetch":
        return JsonResponse({"ok": True, "tags": tags})
    next_url = _safe_next(request)
    if next_url:
        return redirect(next_url)
    return redirect("web:workspace_detail", workspace_slug=workspace.slug)


@login_required
def discovery(request):
    buckets = DiscoveryService.discover(
        request.user, workspace_id=request.GET.get("workspace"), limit=50
    )
    return render(request, "discovery.html", {"buckets": buckets})


@login_required
@require_http_methods(["POST"])
def document_approve(request, pk):
    document = get_object_or_404(Document.objects.select_related("resource"), pk=pk)
    if not _can(request.user, document.resource, Permission.WRITE):
        return HttpResponseForbidden("You do not have write access to this document.")
    DraftService.set_status(
        document=document,
        status=DocumentStatus.APPROVED,
        user=request.user,
        request=request,
    )
    messages.success(request, "Document approved.")
    return redirect("web:document_detail", pk=document.pk)


@login_required
@require_http_methods(["POST"])
def document_reject(request, pk):
    document = get_object_or_404(Document.objects.select_related("resource"), pk=pk)
    if not _can(request.user, document.resource, Permission.WRITE):
        return HttpResponseForbidden("You do not have write access to this document.")
    DraftService.set_status(
        document=document,
        status=DocumentStatus.DRAFT,
        user=request.user,
        request=request,
    )
    messages.success(request, "Document moved back to draft.")
    return redirect("web:document_detail", pk=document.pk)


@login_required
def api_keys(request):
    if request.method == "POST":
        action = request.POST.get("action")
        if action == "create":
            name = request.POST.get("name", "").strip() or "key"
            _key, raw_key = ApiKeyService.create(
                user=request.user, name=name, actor=request.user, request=request
            )
            messages.success(request, f"API key created (copy it now): {raw_key}")
        elif action == "revoke":
            key = get_object_or_404(ApiKey, pk=request.POST.get("key_id"), user=request.user)
            key.revoke()
            messages.success(request, "API key revoked.")
        return redirect("web:api_keys")

    keys = ApiKey.objects.filter(user=request.user).prefetch_related("scopes")
    return render(request, "api_keys.html", {"keys": keys})


@login_required
def audit_dashboard(request):
    events = AuditEvent.objects.select_related("user", "resource")
    if not request.user.is_superuser:
        events = events.filter(user=request.user)
    action = request.GET.get("action", "")
    if action:
        events = events.filter(action=action)
    return render(
        request,
        "audit.html",
        {"events": events[:200], "action": action, "actions": AuditAction.choices},
    )


def _group_member_from_post(request):
    """Resolve the member from `user_id` (the picker) or a typed `username`."""
    user_id = request.POST.get("user_id", "").strip()
    if user_id:
        return User.objects.filter(pk=user_id).first()
    username = request.POST.get("username", "").strip()
    return User.objects.filter(username=username).first() if username else None


@login_required
def groups_admin(request):
    if not request.user.is_staff:
        return HttpResponseForbidden("Staff access required.")
    if request.method == "POST":
        action = request.POST.get("action")
        if action == "create":
            name = request.POST.get("name", "").strip()
            if name:
                _, created = Group.objects.get_or_create(
                    name=name, defaults={"created_by": request.user}
                )
                if created:
                    messages.success(request, f"Group '{name}' created.")
                else:
                    messages.error(request, f"Group '{name}' already exists.")
        elif action == "add_member":
            group = get_object_or_404(Group, pk=request.POST.get("group_id"))
            member = _group_member_from_post(request)
            if member is None:
                messages.error(request, "No such user.")
            else:
                role = request.POST.get("role", GroupMembership.Role.MEMBER)
                if role not in GroupMembership.Role.values:
                    role = GroupMembership.Role.MEMBER
                _, created = GroupMembership.objects.get_or_create(
                    user=member, group=group, defaults={"role": role}
                )
                if created:
                    messages.success(request, f"{_user_label(member)} added to {group.name}.")
                else:
                    messages.error(
                        request, f"{_user_label(member)} is already in {group.name}."
                    )
        elif action == "remove_member":
            GroupMembership.objects.filter(pk=request.POST.get("membership_id")).delete()
        return redirect("web:groups_admin")

    groups = list(Group.objects.prefetch_related("memberships__user").order_by("name"))
    all_users = list(User.objects.order_by("display_name", "username"))
    # Per group: everyone who is not yet a member, so the picker is always full.
    for group in groups:
        member_ids = {m.user_id for m in group.memberships.all()}
        group.candidates = [
            {"id": str(user.pk), "label": _user_label(user)}
            for user in all_users
            if user.pk not in member_ids
        ]
    return render(
        request,
        "groups.html",
        {"groups": groups, "roles": GroupMembership.Role.choices},
    )


def _resolve_subject(subject_type: str, value: str):
    value = (value or "").strip()
    if subject_type == SubjectType.GROUP:
        group = Group.objects.filter(pk=value).first() if "-" in value else None
        if group is None:
            group = Group.objects.filter(name=value).first()
        return group.id if group else None
    user = User.objects.filter(pk=value).first() if "-" in value else None
    if user is None:
        user = User.objects.filter(username=value).first()
    return user.id if user else None


def _user_label(user) -> str:
    if user.display_name and user.display_name != user.username:
        return f"{user.display_name} ({user.username})"
    if user.email:
        return f"{user.username} ({user.email})"
    return user.username


def _subject_matches(user, resource, query: str = "", limit: int = 500) -> dict:
    """Directory of shareable users + groups for the access panel's picker.

    The pool comes from :meth:`PermissionService.grantable_subjects`, never from
    ``User.objects.all()``: the workspace/project owner sees the whole
    directory (they define the audience), anybody else sees only the audience
    that already exists inside the workspace plus their own work group. A
    delegated admin therefore cannot enumerate the company, and cannot widen the
    circle beyond what the owner granted them.
    """
    query = (query or "").strip()
    user_ids, group_ids = PermissionService.grantable_subjects(user, resource)
    users = User.objects.filter(pk__in=user_ids) if user_ids else User.objects.none()
    groups = Group.objects.filter(pk__in=group_ids) if group_ids else Group.objects.none()
    if query:
        users = users.filter(
            Q(username__icontains=query)
            | Q(display_name__icontains=query)
            | Q(email__icontains=query)
        )
        groups = groups.filter(name__icontains=query)
    return {
        "users": [
            {"id": str(row.pk), "label": _user_label(row)}
            for row in users.order_by("display_name", "username")[:limit]
        ],
        "groups": [
            {"id": str(row.pk), "label": f"{row.name} (csoport)"}
            for row in groups.order_by("name")[:limit]
        ],
        "query": query,
        "truncated": users.count() > limit or groups.count() > limit,
    }




@login_required
def resource_permissions(request, resource_id):
    resource = get_object_or_404(Resource, pk=resource_id)
    can_manage = PermissionService.can_manage_acl(request.user, resource)
    can_share = _can(request.user, resource, Permission.WRITE)
    if not (can_manage or can_share):
        return HttpResponseForbidden("Write access required on this resource.")
    is_personal = PermissionService.is_personal(resource)

    if request.method == "POST":
        action = request.POST.get("action")
        if action == "grant":
            raw = request.POST.get("subject_id", "")
            if ":" in raw:
                subject_type, subject_value = raw.split(":", 1)
            else:
                subject_type, subject_value = SubjectType.USER, raw
            permission = request.POST.get("permission", Permission.READ)
            effect = request.POST.get("effect", Effect.ALLOW)
            subject_id = _resolve_subject(subject_type, subject_value)
            if subject_id is None:
                messages.error(request, "Subject not found.")
            else:
                # grant() re-validates the permission level *and* the subject
                # against the sharing pool, so this view cannot become a way
                # around either rule - and neither can the REST or MCP surface,
                # which funnel through the same call.
                try:
                    PermissionService.grant(
                        resource,
                        subject_type=subject_type,
                        subject_id=subject_id,
                        permission=permission,
                        effect=effect,
                        inherit=request.POST.get("inherit") == "on",
                        created_by=request.user,
                    )
                except PermissionDenied as exc:
                    messages.error(request, str(exc) or "Nincs jogosultságod ehhez.")
                else:
                    AuditService.log(
                        AuditAction.CHANGE_PERMISSION,
                        user=request.user,
                        resource=resource,
                        source=AuditSource.WEB,
                        request=request,
                        detail={
                            "action": "grant",
                            "subject_type": subject_type,
                            "permission": permission,
                            "effect": effect,
                        },
                    )
                    messages.success(request, "Access granted.")
        elif action == "revoke":
            raw = request.POST.get("subject_id", "")
            if ":" in raw:
                subject_type, subject_value = raw.split(":", 1)
            else:
                subject_type, subject_value = SubjectType.USER, raw
            subject_id = _resolve_subject(subject_type, subject_value)
            if subject_id is None:
                messages.error(request, "Subject not found.")
            else:
                try:
                    PermissionService.revoke(
                        resource,
                        subject_type=subject_type,
                        subject_id=subject_id,
                        permission=request.POST.get("permission") or None,
                        actor=request.user,
                    )
                except PermissionDenied:
                    messages.error(request, "Ezt a hozzáférést nem szüntetheted meg.")
                else:
                    messages.success(request, "Access revoked.")
        elif action == "create_group":
            # Creating a group *and* handing it read in one step widens the
            # audience of the whole workspace, so it is an owner move.
            if not PermissionService.is_scope_owner(request.user, resource):
                messages.error(request, "Csoportot csak a tulajdonos hozhat létre itt.")
            else:
                name = request.POST.get("group_name", "").strip()
                if not name:
                    messages.error(request, "Group name is required.")
                elif Group.objects.filter(name=name).exists():
                    messages.error(request, "Ilyen nevű csoport már létezik.")
                else:
                    group = Group.objects.create(name=name, created_by=request.user)
                    try:
                        PermissionService.grant(
                            resource,
                            subject_type=SubjectType.GROUP,
                            subject_id=group.id,
                            permission=Permission.READ,
                            created_by=request.user,
                        )
                    except PermissionDenied:
                        group.delete()
                        messages.error(request, "A csoporthoz nem adhattál jogot.")
                    else:
                        messages.success(request, f"Group '{group.name}' created and granted read.")
        elif action == "takeover":
            try:
                OwnershipService.take_over(resource=resource, actor=request.user, request=request)
            except PermissionDenied:
                messages.error(request, "Erre a resource-ra nincs átvételi jogosultságod.")
            else:
                messages.success(request, "Jogosultság átvételve és naplózva.")
        elif action == "transfer":
            new_owner = User.objects.filter(pk=request.POST.get("new_owner")).first()
            if new_owner is None:
                messages.error(request, "Válassz új tulajdonost.")
            else:
                try:
                    OwnershipService.transfer(
                        resource=resource,
                        new_owner=new_owner,
                        actor=request.user,
                        keep_old_access=request.POST.get("keep_access") or None,
                        request=request,
                    )
                except (PermissionDenied, ValidationError) as exc:
                    messages.error(request, str(exc) or "A tulajdon átruházása nem sikerült.")
                else:
                    messages.success(request, f"Tulajdonos: {new_owner.username}")
        elif action == "set_kind":
            try:
                OwnershipService.set_kind(
                    workspace=resource.workspace,
                    kind=request.POST.get("kind", "shared"),
                    actor=request.user,
                    request=request,
                )
            except (PermissionDenied, ValidationError) as exc:
                messages.error(request, str(exc) or "Nem sikerült átalakítani.")
            else:
                messages.success(request, "A workspace típusa frissült.")
        elif action == "toggle_publish_titles":
            workspace = resource.workspace
            if workspace is None or not _can_publish_titles(request.user, workspace):
                messages.error(request, "Ezt csak a workspace tulajdonosa állíthatja.")
            else:
                metadata = dict(workspace.resource.metadata or {})
                metadata["publish_titles"] = request.POST.get("publish_titles") == "on"
                workspace.resource.metadata = metadata
                workspace.resource.save(update_fields=["metadata", "updated_at"])
                AuditService.log(
                    AuditAction.CHANGE_PERMISSION,
                    user=request.user,
                    resource=workspace.resource,
                    workspace=workspace,
                    source=AuditSource.WEB,
                    request=request,
                    detail={
                        "type": "publish_titles",
                        "enabled": metadata["publish_titles"],
                    },
                )
                messages.success(request, "A címek közzététele frissült.")
        elif action == "toggle_no_takeover":
            if not PermissionService.is_scope_owner(request.user, resource):
                messages.error(request, "Ezt csak a tulajdonos állíthatja.")
            else:
                resource.no_takeover = request.POST.get("no_takeover") == "on"
                resource.save(update_fields=["no_takeover", "updated_at"])
                messages.success(request, "Átvétel tiltása frissítve.")
        return redirect(request.POST.get("next") or request.get_full_path())

    owner_rows = []
    scoped_workspace = resource.workspace
    if PermissionService.is_scope_owner(request.user, resource) and not is_personal:
        current_owner = scoped_workspace.owner_id if scoped_workspace else None
        owner_rows = [
            {"id": str(candidate.pk), "label": _user_label(candidate)}
            for candidate in User.objects.filter(is_active=True)
            .exclude(pk=current_owner)
            .order_by("display_name", "username")[:200]
        ]
    return render(
        request,
        "permissions.html",
        {
            "resource": resource,
            "entry_rows": _entry_rows(resource),
            "permissions": Permission.choices,
            "effects": Effect.choices,
            "subject_types": SubjectType.choices,
            "can_admin": can_manage,
            "can_share": can_share,
            "is_owner": PermissionService.is_scope_owner(request.user, resource),
            "is_personal": is_personal,
            "is_workspace_resource": resource.resource_type == ResourceType.WORKSPACE,
            "no_takeover": resource.takeover_locked(),
            "can_take_over": PermissionService.can_take_over(request.user, resource),
            "owner_rows": owner_rows,
            "max_grantable": PermissionService.max_grantable(request.user, resource),
            "publish_titles": (
                _publish_titles(scoped_workspace) if scoped_workspace else False
            ),
            "can_publish_titles": (
                _can_publish_titles(request.user, scoped_workspace)
                if scoped_workspace
                else False
            ),
            # The access panel is tabbed (CSS-only). Forms post `next`, so the
            # `?tab=` survives the redirect and the same panel reopens where you
            # left it instead of jumping back to the first tab.
            "acl_tab": request.GET.get("tab", ""),
            "owner": (
                getattr(resource.workspace, "owner", None)
                if resource.resource_type == ResourceType.WORKSPACE
                else getattr(resource.project, "owner", None)
            ),
            "search": _subject_matches(request.user, resource, request.GET.get("q", "")),
            "access_url": reverse("web:resource_permissions", args=[resource.pk]),
            "standalone": True,
        },
    )


@login_required
@require_http_methods(["POST"])
def resource_takeover(request, resource_id):
    """Audit-only escape hatch for a superuser who needs into a locked resource.

    It writes a normal ADMIN ACL entry and nothing else: the owner does not
    change, so this can never become a way to lock the owner out, and
    ``Resource.no_takeover`` can forbid it outright.
    """
    resource = get_object_or_404(Resource, pk=resource_id)
    try:
        OwnershipService.take_over(resource=resource, actor=request.user, request=request)
    except PermissionDenied as exc:
        messages.error(request, str(exc) or "Nincs átvételi jogosultságod.")
    else:
        messages.success(request, "Jogosultság átvételve és naplózva.")
    return redirect(request.POST.get("next") or reverse("web:dashboard"))


@login_required
@require_http_methods(["POST"])
def workspace_rename(request, workspace_slug):
    """Rename, re-describe or re-slug a workspace.

    Routed through the service so the write is permission-checked and audited -
    the same rule the REST PATCH and the MCP tool follow. After a slug change the
    canonical URL is different, so the redirect goes to the *new* one.
    """
    workspace = get_object_or_404(Workspace, slug=workspace_slug)
    slug_changed = False
    try:
        WorkspaceService.update(
            workspace=workspace,
            name=request.POST.get("name"),
            description=request.POST.get("description"),
            slug=request.POST.get("slug"),
            actor=request.user,
            request=request,
        )
        workspace.refresh_from_db()
        slug_changed = workspace.slug != workspace_slug
    except PermissionDenied:
        messages.error(request, "Nincs jogosultságod módosítani ezt a workspace-t.")
    except ValidationError as exc:
        messages.error(request, "; ".join(exc.messages))
    else:
        if slug_changed:
            messages.warning(
                request,
                "Elmentve. A címke változott, ezért a régi linkek nem működnek.",
            )
        else:
            messages.success(request, "Elmentve.")
    return redirect("web:workspace_detail", workspace_slug=workspace.slug)


@login_required
def personal_workspace(request):
    """Open (or lazily create) the caller's Personal workspace."""
    workspace = PersonalWorkspaceService.get_for(request.user)
    if workspace is None:
        workspace = PersonalWorkspaceService.get_or_create(request.user)
    return redirect(reverse("web:workspace_detail", args=[workspace.slug]))


# ---------------------------------------------------------------------------
# Files
#
# A `File` is bytes on disk plus a row, and until now the only way to reach one
# was the REST FileViewSet. That left the web UI with two holes: there was no
# page that lists files, and nothing served the URLs a rendered document points
# its `<img src>` at - every diagram in the platform was a broken image.
# ---------------------------------------------------------------------------

#: Byte units for the human-readable size column.
_SIZE_UNITS = ("B", "kB", "MB", "GB", "TB")


def _human_size(size) -> str:
    """A byte count as a short Hungarian-readable string (``812 B``, ``1,4 MB``)."""
    value = float(size or 0)
    index = 0
    while value >= 1024 and index < len(_SIZE_UNITS) - 1:
        value /= 1024
        index += 1
    if index == 0:
        return f"{int(value)} B"
    return f"{value:.1f}".replace(".", ",") + f" {_SIZE_UNITS[index]}"


def _is_image_file(stored_file) -> bool:
    """Whether the browser will render this file inline, so it gets a thumbnail.

    Decided on the stored mime type, falling back to the suffix for a file whose
    type could not be guessed at upload time. The suffix list is the same one
    ``apps.documents.embeds`` rewrites document references with, so the page
    previews exactly the set a document can actually embed.
    """
    mime = (stored_file.mime_type or "").lower()
    if mime.startswith("image/"):
        return True
    name = stored_file.name or ""
    suffix = f".{name.rsplit('.', 1)[-1].lower()}" if "." in name else ""
    return suffix in embeds.IMAGE_SUFFIXES


@login_required
@require_http_methods(["GET", "HEAD"])
def file_content(request, pk):
    """Serve one file's bytes, permission-checked, under its stored mime type.

    This is the route :mod:`apps.documents.embeds` rewrites every relative
    ``src``/``href`` to, so a missing or unreadable one puts every diagram in
    every document back to a broken image. It is also the target of every
    thumbnail on the file browser, which is why it is a plain GET and not a
    download.

    Served as a stream from the file handle rather than through
    ``FileService.read_bytes``: this route is hit by the *browser*, once per
    ``<img>`` on a page plus again on every back/forward and cache revalidation,
    and ``read_bytes`` would hold the entire file in memory per concurrent hit.
    A 200 MB PDF opened twice would cost 400 MB resident for bytes the browser
    caches anyway. ``FileResponse`` hands the open handle to the WSGI server
    instead, and ``Content-Length`` comes from the file's own size.
    """
    from apps.files.models import File
    from apps.files.services import FileService

    stored_file = get_object_or_404(
        File.objects.select_related("resource", "workspace", "project"), pk=pk
    )
    # 404, not 403: this URL is what a rendered document embeds, and a 403 would
    # make "you may not see this file" a distinguishable answer - the file URL
    # would become an existence oracle. The UI answers 404 for both everywhere.
    if not _can(request.user, stored_file.resource, Permission.READ):
        raise Http404

    # A row without bytes on disk (a deleted file, a git checkout that was never
    # materialised) is a 404 too - the metadata is not the content.
    try:
        path = FileService.storage_path(stored_file)
        size = path.stat().st_size
        handle = path.open("rb")
    except (OSError, StorageError):
        raise Http404 from None

    AuditService.log(
        AuditAction.READ,
        user=request.user,
        resource=stored_file.resource,
        workspace=stored_file.workspace,
        project=stored_file.project,
        source=AuditSource.WEB,
        request=request,
        detail={
            "type": "file",
            "path": stored_file.path,
            "via": "web:file_content",
            "version": stored_file.current_version,
        },
    )

    as_attachment = request.GET.get("download") == "1"
    response = FileResponse(
        handle,
        content_type=stored_file.mime_type or "application/octet-stream",
        as_attachment=as_attachment,
        filename=stored_file.name,
    )
    response["Content-Length"] = size
    # The stored mime type is the only authority on what this is; without nosniff
    # a browser may second-guess it and run something as script.
    response["X-Content-Type-Options"] = "nosniff"
    # Set explicitly rather than left to FileResponse's guess from the file name:
    # the name and the stored type can disagree, and the stored type is the one
    # that was permission-checked and audited.
    response["Content-Disposition"] = content_disposition_header(
        as_attachment, stored_file.name
    )
    return response


def _upload_rel_path(name: str) -> str:
    """A client-supplied upload name reduced to a safe relative path.

    The same normalisation as ``DocumentService.bulk_create_from_files``: a
    directory upload arrives with the browser's relative path and backslashes, a
    Windows name can carry a drive prefix, and ``.``/``..`` segments have to go -
    the storage adapter is the wrong place to find out a name was hostile.
    """
    raw = (name or "").strip().replace("\\", "/")
    if len(raw) > 1 and raw[1] == ":":
        raw = raw[2:]
    segments = [
        segment for segment in raw.lstrip("/").split("/") if segment not in {"", ".", ".."}
    ]
    return "/".join(segments)


def _file_browser_rows(workspace, node, project, user) -> list[dict]:
    """Readable attachments of a scope or a node, grouped by their folder.

    ``File`` lives *in* a folder now, so a node lists its own subtree's
    attachments (matched through the folder chain), while the workspace root
    lists the files attached directly to the workspace/project scope.

    Read and delete are resolved in two bulk passes rather than one check per
    file: a permission check walks the resource's ancestor chain, so a folder
    with 500 files would otherwise be 500 round trips to re-answer the same
    question. Files the caller may not read are dropped here - the browser page
    must not become a way to learn that a file exists.
    """
    from apps.files.models import File

    if node is not None:
        # A file's container is its Resource.parent: the node itself for a
        # root-level attachment (its `folder` FK is NULL), a descendant folder
        # for a nested one. Matching on `resource__parent_id` catches both,
        # where matching on `folder` would silently drop the root-level files.
        ids = [folder.resource_id for folder in _subtree_folders(node)]
        scope_query = File.objects.filter(resource__parent_id__in=ids)
    else:
        scope_query = File.objects.filter(workspace=workspace, project=project)
    stored_files = list(scope_query.select_related("resource").order_by("path"))
    resource_ids = [stored_file.resource_id for stored_file in stored_files]
    readable = set(
        PermissionService.allowed_resource_ids(user, resource_ids, Permission.READ)
    )
    deletable = set(
        PermissionService.allowed_resource_ids(user, resource_ids, Permission.DELETE)
    )

    root: dict = {"children": {}, "files": []}
    for stored_file in stored_files:
        if stored_file.resource_id not in readable:
            continue
        node = root
        prefix = ""
        for segment in (stored_file.path or stored_file.name).split("/")[:-1]:
            prefix = f"{prefix}/{segment}" if prefix else segment
            node["children"].setdefault(segment, {"children": {}, "files": [], "path": prefix})
            node = node["children"][segment]
        node["files"].append(stored_file)

    rows: list[dict] = []

    def walk(node: dict, depth: int) -> None:
        for name in sorted(node["children"]):
            child = node["children"][name]
            rows.append({"type": "dir", "name": name, "depth": depth, "path": child["path"]})
            walk(child, depth + 1)
        for stored_file in node["files"]:
            rows.append(
                {
                    "type": "file",
                    "name": stored_file.name,
                    "depth": depth,
                    "path": stored_file.path,
                    "file": stored_file,
                    "size": _human_size(stored_file.size),
                    "is_image": _is_image_file(stored_file),
                    "can_delete": stored_file.resource_id in deletable,
                }
            )

    walk(root, 0)
    return rows


def _file_browser_redirect(workspace, node):
    """The attachments URL of this scope or node - where an upload/delete returns."""
    if node is not None:
        return reverse("web:node_files", args=[workspace.slug, node.tree_path()])
    return reverse("web:workspace_files", args=[workspace.slug])


def _file_browser_post(request, workspace, node, project):
    """Upload or delete, then go back to the page the form was posted from."""
    from apps.files.models import File
    from apps.files.services import FileService

    target = node.resource if node is not None else workspace.resource
    back = _file_browser_redirect(workspace, node)

    if request.POST.get("action") == "delete":
        stored_file = get_object_or_404(
            File.objects.select_related("resource", "workspace", "project"),
            pk=request.POST.get("file"),
        )
        # Delete is checked on the file's own resource, not on the container: a
        # write grant on a workspace is not a grant to remove every file in it.
        if not _can(request.user, stored_file.resource, Permission.DELETE):
            messages.error(request, "Nincs törlési jogosultságod ehhez a fájlhoz.")
            return redirect(back)
        FileService.delete(stored_file=stored_file, user=request.user, request=request)
        messages.success(request, f"Törölve: {stored_file.path}")
        return redirect(back)

    if not _can(request.user, target, Permission.WRITE):
        messages.error(request, "Nincs írási jogosultságod ide.")
        return redirect(back)

    uploads = [(upload.name, upload.read()) for upload in request.FILES.getlist("files")]
    if not uploads:
        messages.error(request, "Válassz ki legalább egy fájlt.")
        return redirect(back)

    # Same shape as `document_bulk_upload`: an optional folder field, many files,
    # one stored object each, duplicates reported instead of aborting the batch.
    # On a node page the node's own scope-relative path is the prefix, so an
    # upload lands *in* the folder being viewed, not at the scope root. The rest
    # is prepended exactly as `bulk_create_from_files` does, so a directory
    # upload keeps its sub-paths and the two upload forms behave the same way.
    folder = (request.POST.get("folder") or "").strip().strip("/")
    base = _scope_prefix(node)
    created = 0
    for name, data in uploads:
        rel_path = _upload_rel_path(name)
        if not rel_path:
            continue
        upload_path = "/".join(part for part in (base, folder, rel_path) if part)
        try:
            FileService.create(
                workspace=workspace,
                project=project,
                name=Path(rel_path).name,
                path=upload_path,
                data=data,
                created_by=request.user,
                source=ChangeSource.WEB,
                request=request,
            )
        except ValidationError as exc:
            messages.warning(f"{rel_path}: {'; '.join(exc.messages)}")
            continue
        created += 1
    if created:
        messages.success(request, f"{created} fájl betöltve.")
    return redirect(back)


@login_required
@require_http_methods(["GET", "POST"])
def file_browser(request, workspace_slug, tree_path=None):
    """List the attachments of the workspace root or any node, and manage them."""
    workspace = get_object_or_404(Workspace, slug=workspace_slug)
    node, project, container = _node_scope(workspace, tree_path)
    # can_browse, not check, exactly as workspace_detail/folder_detail do: a
    # grant on a single file inside has to be reachable, or the page that would
    # show it is never rendered.
    if not PermissionService.can_browse(request.user, container):
        raise Http404

    if request.method == "POST":
        return _file_browser_post(request, workspace, node, project)

    rows = _file_browser_rows(workspace, node, project, request.user)
    return render(
        request,
        "file_browser.html",
        {
            "workspace": workspace,
            "project": project,
            "node": _crumb_node(node),
            "rows": rows,
            "can_write": _can(request.user, container, Permission.WRITE),
            "file_count": sum(1 for row in rows if row["type"] == "file"),
            "detail_url": _file_browser_redirect(workspace, node),
        },
    )
