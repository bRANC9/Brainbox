"""Knowledge-centric web UI. Every view goes through the permission engine."""

from __future__ import annotations

import difflib

import markdown as md
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.http import Http404, HttpResponseForbidden
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_http_methods

from apps.documents.frontmatter import parse_frontmatter
from apps.documents.models import ChangeSource, Document, DocumentStatus, DocumentVersion
from apps.documents.services import DocumentService
from apps.git.services import GitService
from apps.permissions.constants import Permission
from apps.permissions.services import PermissionService
from apps.workspaces.models import Project, Workspace

MARKDOWN_EXTENSIONS = ["fenced_code", "tables", "toc", "sane_lists", "codehilite", "nl2br"]


def _can(user, resource, permission: str) -> bool:
    return PermissionService.check(user, resource, permission)


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
        for workspace in Workspace.objects.select_related("resource")
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
        {"workspaces": workspaces, "recent_documents": recent_documents},
    )


@login_required
def workspace_detail(request, workspace_slug):
    workspace = get_object_or_404(Workspace, slug=workspace_slug)
    if not _can(request.user, workspace.resource, Permission.READ):
        raise Http404

    projects = [
        project
        for project in workspace.project_set.select_related("resource")
        if _can(request.user, project.resource, Permission.READ)
    ]
    documents = [
        document
        for document in workspace.documents.filter(project__isnull=True).select_related("resource")
        if _can(request.user, document.resource, Permission.READ)
    ]
    can_write = _can(request.user, workspace.resource, Permission.WRITE)
    context = {
        "workspace": workspace,
        "projects": projects,
        "documents": documents,
        "can_write": can_write,
        **_git_panel(workspace),
    }
    return render(request, "workspace_detail.html", context)


@login_required
def project_detail(request, workspace_slug, project_slug):
    workspace = get_object_or_404(Workspace, slug=workspace_slug)
    project = get_object_or_404(Project, workspace=workspace, slug=project_slug)
    if not _can(request.user, project.resource, Permission.READ):
        raise Http404

    documents = [
        document
        for document in project.documents.select_related("resource")
        if _can(request.user, document.resource, Permission.READ)
    ]
    can_write = _can(request.user, project.resource, Permission.WRITE)
    context = {
        "workspace": workspace,
        "project": project,
        "documents": documents,
        "can_write": can_write,
        **_git_panel(workspace, project),
    }
    return render(request, "project_detail.html", context)


@login_required
def document_detail(request, pk):
    document = get_object_or_404(Document.objects.select_related("workspace", "project"), pk=pk)
    if not _can(request.user, document.resource, Permission.READ):
        raise Http404

    content = DocumentService.read_content(document)
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
            "can_write": _can(request.user, document.resource, Permission.WRITE),
        },
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
        document = DocumentService.create(
            workspace=workspace,
            project=project,
            title=request.POST.get("title", ""),
            path=request.POST.get("path") or None,
            content=request.POST.get("content", ""),
            summary=request.POST.get("summary", ""),
            status=request.POST.get("status") or None,
            created_by=request.user,
            source=ChangeSource.WEB,
            request=request,
        )
        messages.success(request, f"Document '{document.title}' created.")
        return redirect("web:document_detail", pk=document.pk)

    return render(
        request,
        "document_form.html",
        {
            "workspace": workspace,
            "project": project,
            "document": None,
            "content": "",
            "statuses": DocumentStatus.choices,
        },
    )


@login_required
@require_http_methods(["GET", "POST"])
def document_edit(request, pk):
    document = get_object_or_404(Document.objects.select_related("workspace", "project"), pk=pk)
    if not _can(request.user, document.resource, Permission.WRITE):
        return HttpResponseForbidden("You do not have write access to this document.")

    if request.method == "POST":
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
