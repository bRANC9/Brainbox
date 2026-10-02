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
from django.http import Http404, HttpResponse, HttpResponseForbidden
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views.decorators.http import require_http_methods

from apps.accounts.models import ApiKey
from apps.accounts.services import ApiKeyService
from apps.audit.models import AuditAction, AuditEvent, AuditSource
from apps.audit.services import AuditService
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


def _project_queryset_with_counts():
    return Project.objects.annotate(
        document_count=Count("documents", distinct=True)
    ).order_by("name")


def _render_markdown(content: str) -> str:
    _frontmatter, body = parse_frontmatter(content)
    return md.markdown(body, extensions=MARKDOWN_EXTENSIONS)


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
    """
    from apps.tags.services import filter_documents_by_tag, tags_for

    documents = [
        doc
        for doc in Document.objects.filter(workspace=workspace, project=project)
        if _can(user, doc.resource, Permission.READ)
    ]
    if tag_filter:
        documents = filter_documents_by_tag(documents, tag_filter)

    folders = list(
        DocumentFolder.objects.filter(workspace=workspace, project=project).select_related(
            "resource"
        )
    )
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
        if folder.resource_id in visible:
            folder_paths.add(folder.path)
            if folder.resource_id in readable:
                readable_paths.add(folder.path)
    for document in documents:
        parts = (document.path or "").split("/")[:-1]
        for index in range(1, len(parts) + 1):
            path = "/".join(parts[:index])
            folder_paths.add(path)
            readable_paths.add(path)

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
                    "can_read": path in readable_paths,
                    "folder": folder_by_path.get(path),
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
    """Right-click menu operations on a folder: rename / delete (by path)."""
    import json as _json

    from django.core.exceptions import ValidationError
    from django.http import JsonResponse

    from apps.documents.folders import delete_folder, ensure_folder, rename_folder

    try:
        payload = _json.loads(request.body.decode("utf-8") or "{}")
    except ValueError:
        return JsonResponse({"ok": False, "error": "rossz JSON"}, status=400)

    workspace = get_object_or_404(Workspace, slug=payload.get("workspace"))
    project = (
        get_object_or_404(Project, workspace=workspace, slug=payload.get("project"))
        if payload.get("project")
        else None
    )
    resource = project.resource if project else workspace.resource
    if not _can(request.user, resource, Permission.WRITE):
        return JsonResponse({"ok": False, "error": "Nincs írási jogosultságod."}, status=403)

    path = (payload.get("path") or "").strip("/")
    op = payload.get("op")
    if not path:
        return JsonResponse({"ok": False, "error": "nincs mappa megadva"}, status=400)

    # Renaming or deleting an existing folder needs write on *that* folder; the
    # workspace-level check above only covers creating a new one.
    existing = DocumentFolder.objects.filter(
        workspace=workspace, project=project, path=path
    ).select_related("resource").first()
    if existing is not None and not _can(request.user, existing.resource, Permission.WRITE):
        return JsonResponse({"ok": False, "error": "Nincs írási jogosultságod."}, status=403)

    try:
        folder = ensure_folder(workspace, project, path, created_by=request.user)
        if op == "rename":
            name = (payload.get("name") or "").strip("/")
            if not name:
                return JsonResponse({"ok": False, "error": "Üres név."}, status=400)
            if "/" in name:
                new_path = name
            else:
                parent = path.rsplit("/", 1)[0] if "/" in path else ""
                new_path = f"{parent}/{name}" if parent else name
            rename_folder(folder=folder, path=new_path)
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
    """Drag-and-drop endpoint: move a document or a folder into a target folder."""
    import json as _json

    from django.core.exceptions import ValidationError
    from django.http import JsonResponse

    from apps.documents.folders import DocumentFolder, move_folder
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
            filename = Path(document.path).name
            new_path = f"{target}/{filename}" if target else filename
            DocumentService.move(document, new_path, user=request.user, request=request)
        elif kind == "folder":
            folder = get_object_or_404(DocumentFolder, pk=payload.get("id"))
            # Folders carry their own Resource now, so the check is on the folder
            # and not on the workspace it happens to live in: a write grant on
            # the container is not a write grant on every folder inside it.
            denied = _require_write_or_403(request, folder.resource)
            if denied:
                return denied
            move_folder(folder=folder, new_parent=target, user=request.user)
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


@login_required
def folder_create(request, workspace_slug, project_slug=None):
    """Create a folder inside a workspace/project."""
    from django.core.exceptions import ValidationError

    from apps.documents.folders import create_folder

    workspace = get_object_or_404(Workspace, slug=workspace_slug)
    project = (
        get_object_or_404(Project, workspace=workspace, slug=project_slug)
        if project_slug
        else None
    )
    target = project.resource if project else workspace.resource
    if not _can(request.user, target, Permission.WRITE):
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
                target = f"{parent}/{raw}" if parent else raw
                try:
                    create_folder(
                        workspace=workspace, project=project, path=target, created_by=request.user
                    )
                    ok += 1
                except ValidationError as exc:
                    failed.append(f"{raw}: {'; '.join(exc.messages)}")
            if ok:
                messages.success(request, f"{ok} mappa létrehozva.")
            for item in failed:
                messages.warning(item)
        if project:
            return redirect(
                "web:project_detail", workspace_slug=workspace.slug, project_slug=project.slug
            )
        return redirect("web:workspace_detail", workspace_slug=workspace.slug)

    return render(
        request,
        "folder_form.html",
        {
            "workspace": workspace,
            "project": project,
            "parent": (request.GET.get("parent") or "").strip("/"),
        },
    )


@login_required
def search(request):
    query = request.GET.get("q", "").strip()
    mode = request.GET.get("mode", "hybrid")
    if mode not in {"hybrid", "text", "semantic"}:
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
    return render(
        request, "search.html", {"query": query, "mode": mode, "results": results}
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

    projects = [
        project
        for project in _project_queryset_with_counts()
        .select_related("resource")
        .filter(workspace=workspace)
        if _can(request.user, project.resource, Permission.READ)
    ]
    documents = [
        document
        for document in workspace.documents.filter(project__isnull=True).select_related("resource")
        if _can(request.user, document.resource, Permission.READ)
    ]
    can_write = _can(request.user, workspace.resource, Permission.WRITE)
    tag_filter = request.GET.get("tag", "")
    tree_rows = _folder_tree_rows(workspace, None, request.user, tag_filter)
    # A workspace's own tree only ever holds content that sits at the workspace
    # root. Knowledge normally lives in projects, so an empty tree here used to
    # read as "this workspace is empty" while it held ten documents. Say where
    # the content actually is instead of pretending.
    content_in_projects = bool(projects) and not any(
        row["type"] == "doc" for row in tree_rows
    )
    context = {
        "workspace": workspace,
        "projects": projects,
        "documents": documents,
        "can_write": can_write,
        "is_personal": workspace.is_personal,
        "owner": workspace.owner,
        "can_rename": _can(request.user, workspace.resource, Permission.ADMIN),
        "content_in_projects": content_in_projects,
        **_access_context(request, workspace.resource),
        "tree_rows": tree_rows,
        "tag_filter": tag_filter,
        "available_tags": _available_tags(workspace, None),
        **_git_panel(workspace),
    }
    return render(request, "workspace_detail.html", context)


@login_required
def project_detail(request, workspace_slug, project_slug):
    workspace = get_object_or_404(Workspace, slug=workspace_slug)
    project = get_object_or_404(Project, workspace=workspace, slug=project_slug)
    if not PermissionService.can_browse(request.user, project.resource):
        raise Http404

    documents = [
        document
        for document in project.documents.select_related("resource")
        if _can(request.user, document.resource, Permission.READ)
    ]
    can_write = _can(request.user, project.resource, Permission.WRITE)
    tag_filter = request.GET.get("tag", "")
    context = {
        "workspace": workspace,
        "project": project,
        "documents": documents,
        "can_write": can_write,
        "owner": project.owner or workspace.owner,
        **_access_context(request, project.resource),
        "tree_rows": _folder_tree_rows(workspace, project, request.user, tag_filter),
        "tag_filter": tag_filter,
        "available_tags": _available_tags(workspace, project),
        **_git_panel(workspace, project),
    }
    return render(request, "project_detail.html", context)


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
            "rendered_html": _render_markdown(content),
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
def document_bulk_upload(request, workspace_slug, project_slug=None):
    """Upload many .md files into a folder of a workspace/project at once."""
    from apps.documents.folders import ensure_folder
    from apps.documents.services import DocumentService

    workspace = get_object_or_404(Workspace, slug=workspace_slug)
    project = (
        get_object_or_404(Project, workspace=workspace, slug=project_slug)
        if project_slug
        else None
    )
    target = project.resource if project else workspace.resource
    if not _can(request.user, target, Permission.WRITE):
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
        if project:
            return redirect(
                "web:project_detail", workspace_slug=workspace.slug, project_slug=project.slug
            )
        return redirect("web:workspace_detail", workspace_slug=workspace.slug)

    folders = [
        row["path"]
        for row in _folder_tree_rows(workspace, project, request.user)
        if row["type"] == "dir"
    ]
    return render(
        request,
        "document_bulk_form.html",
        {"workspace": workspace, "project": project, "folders": folders},
    )


@login_required
@require_http_methods(["GET", "POST"])
def document_create(request, workspace_slug, project_slug=None):
    workspace = get_object_or_404(Workspace, slug=workspace_slug)
    project = None
    if project_slug:
        project = get_object_or_404(Project, workspace=workspace, slug=project_slug)
    target = project.resource if project else workspace.resource
    if not _can(request.user, target, Permission.WRITE):
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
def workspace_git_pull(request, workspace_slug):
    workspace = get_object_or_404(Workspace, slug=workspace_slug)
    if not _can(request.user, workspace.resource, Permission.WRITE):
        return HttpResponseForbidden("You do not have write access here.")
    repository = GitService.repository_for(workspace=workspace)
    if repository is None:
        messages.error(request, "This workspace is not Git-backed.")
    else:
        try:
            result = GitService.pull_repository(repository, user=request.user, request=request)
            messages.success(request, f"Git sync complete: {result}")
        except Exception as exc:  # noqa: BLE001 - surface the message in the UI
            messages.error(request, f"Git sync failed: {exc}")
    return redirect("web:workspace_detail", workspace_slug=workspace.slug)


@login_required
@require_http_methods(["POST"])
def project_git_pull(request, workspace_slug, project_slug):
    workspace = get_object_or_404(Workspace, slug=workspace_slug)
    project = get_object_or_404(Project, workspace=workspace, slug=project_slug)
    if not _can(request.user, project.resource, Permission.WRITE):
        return HttpResponseForbidden("You do not have write access here.")
    repository = GitService.repository_for(workspace=workspace, project=project)
    if repository is None:
        messages.error(request, "This project is not Git-backed.")
    else:
        try:
            result = GitService.pull_repository(repository, user=request.user, request=request)
            messages.success(request, f"Git sync complete: {result}")
        except Exception as exc:  # noqa: BLE001 - surface the message in the UI
            messages.error(request, f"Git sync failed: {exc}")
    return redirect(
        "web:project_detail", workspace_slug=workspace.slug, project_slug=project.slug
    )


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
    return redirect("web:document_detail", pk=document.pk)


@login_required
@require_http_methods(["POST"])
def folder_tags(request):
    """Attach/detach tags on a folder (affects its subtree when filtering)."""
    from django.http import JsonResponse

    from apps.documents.models import DocumentFolder
    from apps.tags.services import tag_target, untag_target

    workspace = get_object_or_404(Workspace, slug=request.POST.get("workspace"))
    project = (
        get_object_or_404(Project, workspace=workspace, slug=request.POST.get("project"))
        if request.POST.get("project")
        else None
    )
    resource = project.resource if project else workspace.resource
    if not _can(request.user, resource, Permission.WRITE):
        return HttpResponseForbidden("Nincs írási jogosultságod ehhez a mappához.")

    folder = DocumentFolder.objects.filter(
        workspace=workspace, project=project, path=(request.POST.get("path") or "").strip("/")
    ).first()
    if folder is None:
        from apps.documents.folders import ensure_folder

        folder = ensure_folder(workspace, project, request.POST.get("path") or "", created_by=request.user)

    names = [
        part.strip()
        for part in (request.POST.get("tags") or "").replace(",", " ").split()
        if part.strip()
    ]
    action = request.POST.get("action")
    tags = untag_target(folder, names) if action == "remove" else tag_target(folder, names, user=request.user)

    if request.headers.get("X-Requested-With") == "fetch":
        return JsonResponse({"ok": True, "tags": tags})
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
