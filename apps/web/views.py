"""Knowledge-centric web UI. Every view goes through the permission engine."""

from __future__ import annotations

import difflib

import markdown as md
from django.contrib import messages
from django.contrib.auth import get_user_model
from django.contrib.auth.decorators import login_required
from django.http import Http404, HttpResponse, HttpResponseForbidden
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views.decorators.http import require_http_methods

from apps.accounts.models import ApiKey
from apps.accounts.services import ApiKeyService
from apps.audit.models import AuditAction, AuditEvent, AuditSource
from apps.audit.services import AuditService
from apps.documents.frontmatter import parse_frontmatter
from apps.documents.models import ChangeSource, Document, DocumentStatus, DocumentVersion
from apps.documents.services import DocumentService
from apps.git.services import GitService
from apps.groups.models import Group, GroupMembership
from apps.knowledge.services import DiscoveryService, DraftService, GraphService
from apps.permissions.constants import Effect, Permission, SubjectType
from apps.permissions.services import PermissionService
from apps.resources.models import Resource
from apps.search.services import SearchService
from apps.workspaces.models import Project, Workspace

User = get_user_model()

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
def search(request):
    query = request.GET.get("q", "").strip()
    mode = request.GET.get("mode", "hybrid")
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


def _access_context(request, resource) -> dict:
    """Context for the shared access (ACL) panel, or {} when not permitted."""
    can_admin = PermissionService.check(request.user, resource, Permission.ADMIN)
    can_share = PermissionService.check(request.user, resource, Permission.WRITE)
    if not (can_admin or can_share):
        return {}
    entries = resource.acl_entries.all()
    entry_rows = []
    for entry in entries:
        label = entry.subject_id
        if entry.subject_type == SubjectType.GROUP:
            group = Group.objects.filter(pk=entry.subject_id).first()
            label = group.name if group else "(törölt csoport)"
        else:
            subject = User.objects.filter(pk=entry.subject_id).first()
            label = subject.username if subject else "(törölt user)"
        entry_rows.append({"entry": entry, "label": label})
    return {
        "access_entries": entry_rows,
        "access_can_admin": can_admin,
        "access_can_share": can_share,
        "access_url": reverse("web:resource_permissions", args=[resource.pk]),
        "access_search": _subject_matches(request.GET.get("access_q", "")),
    }


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
        "can_admin": _can(request.user, workspace.resource, Permission.ADMIN),
        **_git_panel(workspace),
        **_access_context(request, workspace.resource),
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
        **_access_context(request, project.resource),
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
            "related": GraphService.neighbors(request.user, document.resource, depth=1),
            "can_write": _can(request.user, document.resource, Permission.WRITE),
            "can_admin": _can(request.user, document.resource, Permission.ADMIN),
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


@login_required
def calendar(request):
    """Month view of deadlines."""
    import calendar as calmod
    from datetime import date

    today = date.today()
    year = int(request.GET.get("year", today.year))
    month = int(request.GET.get("month", today.month))
    cal = calmod.Calendar(firstweekday=0)
    if not 1 <= month <= 12:
        year, month = today.year, today.month
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
            "month_name": calmod.month_name[month],
            "month_grid": month_grid,
            "days": days,
            "today": today,
            "prev_year": prev_year,
            "prev_month": prev_month,
            "next_year": next_year,
            "next_month": next_month,
            "weekday_names": ["Hét", "Ked", "Sze", "Csü", "Pén", "Szo", "Vas"],
        },
    )


@login_required
def agenda(request):
    """Agenda view: overdue + upcoming deadlines grouped by day."""
    from datetime import timedelta

    from django.utils import timezone as djtz

    today = djtz.localdate()
    horizon = int(request.GET.get("days", 30))
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
    horizon = int(request.GET.get("days", 365))
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


@login_required
def groups_admin(request):
    if not request.user.is_staff:
        return HttpResponseForbidden("Staff access required.")
    if request.method == "POST":
        action = request.POST.get("action")
        if action == "create":
            name = request.POST.get("name", "").strip()
            if name:
                Group.objects.get_or_create(name=name, defaults={"created_by": request.user})
        elif action == "add_member":
            group = get_object_or_404(Group, pk=request.POST.get("group_id"))
            member = User.objects.filter(username=request.POST.get("username", "")).first()
            if member is None:
                messages.error(request, "No such user.")
            else:
                GroupMembership.objects.get_or_create(user=member, group=group)
        elif action == "remove_member":
            GroupMembership.objects.filter(pk=request.POST.get("membership_id")).delete()
        return redirect("web:groups_admin")

    groups = Group.objects.prefetch_related("memberships__user")
    return render(request, "groups.html", {"groups": groups})


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


def _subject_matches(query: str, limit: int = 8) -> dict:
    """Directory search for the access panel (users + groups by name)."""
    query = (query or "").strip()
    if not query:
        return {"users": [], "groups": []}
    users = list(
        User.objects.filter(username__icontains=query)[:limit]
    ) + list(User.objects.filter(display_name__icontains=query)[:limit])
    seen, user_rows = set(), []
    for user in users:
        if user.pk in seen:
            continue
        seen.add(user.pk)
        user_rows.append({"id": str(user.pk), "label": f"{user.username} ({user.display_name or user.email})" if (user.display_name or user.email) else user.username})
    groups = [
        {"id": str(group.pk), "label": f"{group.name} (csoport)"}
        for group in Group.objects.filter(name__icontains=query)[:limit]
    ]
    return {"users": user_rows[:limit], "groups": groups[:limit]}


@login_required
def resource_permissions(request, resource_id):
    resource = get_object_or_404(Resource, pk=resource_id)
    can_admin = PermissionService.check(request.user, resource, Permission.ADMIN)
    can_share = PermissionService.check(request.user, resource, Permission.WRITE)
    if not (can_admin or can_share):
        return HttpResponseForbidden("Write access required on this resource.")

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
            elif not PermissionService.can_grant(request.user, resource, permission, effect):
                messages.error(request, "You cannot grant that permission here.")
            else:
                PermissionService.grant(
                    resource,
                    subject_type=subject_type,
                    subject_id=subject_id,
                    permission=permission,
                    effect=effect,
                    inherit=request.POST.get("inherit") == "on",
                    created_by=request.user,
                )
                AuditService.log(
                    AuditAction.CHANGE_PERMISSION,
                    user=request.user,
                    resource=resource,
                    source=AuditSource.WEB,
                    request=request,
                    detail={"action": "grant", "subject_type": subject_type, "permission": permission, "effect": effect},
                )
                messages.success(request, "Access granted.")
        elif action == "revoke":
            if not can_admin:
                messages.error(request, "Only an admin can revoke access.")
            else:
                raw = request.POST.get("subject_id", "")
                _, subject_id = raw.split(":", 1) if ":" in raw else (None, raw)
                PermissionService.revoke(
                    resource,
                    subject_type=request.POST.get("subject_type", SubjectType.USER),
                    subject_id=subject_id,
                    permission=request.POST.get("permission") or None,
                )
                messages.success(request, "Access revoked.")
        elif action == "create_group":
            if not can_admin:
                messages.error(request, "Only an admin can create groups here.")
            else:
                name = request.POST.get("group_name", "").strip()
                if name:
                    group, _ = Group.objects.get_or_create(name=name, defaults={"created_by": request.user})
                    PermissionService.grant(
                        resource,
                        subject_type=SubjectType.GROUP,
                        subject_id=group.id,
                        permission=Permission.READ,
                        created_by=request.user,
                    )
                    messages.success(request, f"Group '{group.name}' created and granted read.")
                else:
                    messages.error(request, "Group name is required.")
        return redirect(f"{request.POST.get('next') or request.get_full_path()}")

    # Decorate entries with a readable label + honour the grant ceiling.
    entries = resource.acl_entries.all().select_related("created_by")
    entry_rows = []
    for entry in entries:
        label = entry.subject_id
        if entry.subject_type == SubjectType.GROUP:
            group = Group.objects.filter(pk=entry.subject_id).first()
            label = group.name if group else "(törölt csoport)"
        else:
            subject = User.objects.filter(pk=entry.subject_id).first()
            label = subject.username if subject else "(törölt user)"
        entry_rows.append({"entry": entry, "label": label})

    return render(
        request,
        "permissions.html",
        {
            "resource": resource,
            "entry_rows": entry_rows,
            "permissions": Permission.choices,
            "effects": Effect.choices,
            "subject_types": SubjectType.choices,
            "can_admin": can_admin,
            "can_share": can_share,
            "search": _subject_matches(request.GET.get("q", "")),
            "access_url": reverse("web:resource_permissions", args=[resource.pk]),
            "standalone": True,
        },
    )
