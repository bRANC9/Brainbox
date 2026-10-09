"""MCP tool registry.

Every tool resolves access through the central PermissionService. Tools never
touch PostgreSQL/storage directly; they call the same application services the
REST API uses.
"""

from __future__ import annotations

import json
import re
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any, Callable

from django.conf import settings
from django.core.exceptions import PermissionDenied, ValidationError

from apps.accounts.models import User
from apps.audit.models import AuditAction, AuditSource
from apps.audit.services import AuditService
from apps.documents.models import ChangeSource, Document, DocumentStatus
from apps.documents.services import DocumentService
from apps.gateway.models import GatewayKind
from apps.gateway.services import UNSET, GatewayService, RateLimited, parse_sse_json
from apps.git.git_cli import GitError
from apps.git.models import GitRepository
from apps.git.services import GitService
from apps.knowledge.services import (
    DiscoveryService,
    DraftService,
    GraphService,
    QualityService,
    publishes_summary,
)
from apps.permissions.constants import Permission
from apps.permissions.services import PermissionService
from apps.resources.models import Resource
from apps.search.services import SearchService
from apps.secrets.models import Secret
from apps.secrets.services import SecretService
from apps.workspaces.models import Project, Workspace
from apps.workspaces.ownership import OwnershipService
from apps.workspaces.personal import PersonalWorkspaceService
from apps.workspaces.services import ProjectService


class ToolError(RuntimeError):
    """A user-facing tool error (returned as isError, not a protocol error)."""


@dataclass
class ToolContext:
    user: Any
    api_key: Any = None
    request: Any = None


_REGISTRY: dict[str, dict] = {}


def tool(name: str, description: str, schema: dict | None = None):
    def decorator(func: Callable):
        _REGISTRY[name] = {
            "name": name,
            "description": description,
            "inputSchema": schema or {"type": "object", "properties": {}},
            "handler": func,
        }
        return func

    return decorator


def definitions() -> list[dict]:
    return [
        {key: entry[key] for key in ("name", "description", "inputSchema")}
        for entry in _REGISTRY.values()
    ]


def call(name: str, ctx: ToolContext, arguments: dict) -> Any:
    entry = _REGISTRY.get(name)
    if entry is None:
        raise ToolError(f"Unknown tool: {name}")
    return entry["handler"](ctx, arguments or {})


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _require(ctx: ToolContext, resource, permission: str) -> None:
    if resource is None or not PermissionService.check(
        ctx.user, resource, permission, api_key=ctx.api_key
    ):
        raise ToolError("Permission denied.")


def _may_read(ctx: ToolContext, resource) -> bool:
    return PermissionService.check(ctx.user, resource, Permission.READ, api_key=ctx.api_key)


def _get_document(document_id) -> Document:
    try:
        return Document.objects.select_related("workspace", "project", "resource").get(
            pk=document_id
        )
    except (Document.DoesNotExist, ValidationError, ValueError, TypeError) as exc:
        raise ToolError(f"Document '{document_id}' not found.") from exc


def _get_folder(value):
    """Resolve a folder node by id (a project is a folder node too)."""
    from apps.documents.models import DocumentFolder

    try:
        return DocumentFolder.objects.select_related(
            "resource", "workspace", "project", "container"
        ).get(pk=value)
    except (DocumentFolder.DoesNotExist, ValidationError, ValueError, TypeError) as exc:
        raise ToolError(f"Folder '{value}' not found.") from exc


def _get_workspace(value) -> Workspace:
    queryset = Workspace.objects.select_related("resource")
    try:
        return queryset.get(pk=value) if str(value).count("-") >= 4 else queryset.get(slug=value)
    except (Workspace.DoesNotExist, ValidationError, ValueError):
        raise ToolError(f"Workspace '{value}' not found.") from None


def _get_project(value) -> Project:
    try:
        return Project.objects.select_related("resource", "workspace").get(pk=value)
    except (Project.DoesNotExist, ValidationError, ValueError):
        raise ToolError(f"Project '{value}' not found.") from None


def _get_repository(value) -> GitRepository:
    try:
        return GitRepository.objects.select_related("workspace", "project", "resource").get(pk=value)
    except (GitRepository.DoesNotExist, ValidationError, ValueError):
        raise ToolError(f"Git repository '{value}' not found.") from None


def _get_user(value) -> User:
    """Resolve a user by uuid or username (ownership tools)."""
    if not value:
        raise ToolError("A user id or username is required.")
    queryset = User.objects.filter(is_active=True)
    try:
        return queryset.get(pk=value)
    except (User.DoesNotExist, ValidationError, ValueError, TypeError):
        pass
    try:
        return queryset.get(username=str(value))
    except User.DoesNotExist:
        raise ToolError(f"User '{value}' not found.") from None


def _service_error(exc: Exception) -> ToolError:
    """Turn a service-layer refusal into a user-facing tool error.

    The services own the wording of their own rules (and some of it is
    Hungarian), so the message is passed through instead of being replaced here.
    """
    if isinstance(exc, ValidationError):
        return ToolError("; ".join(exc.messages))
    return ToolError(str(exc) or exc.__class__.__name__)


def _readable_graph_rows(
    ctx: ToolContext, rows: list[dict], *, include_inaccessible: bool = False
) -> list[dict]:
    """Drop rows whose resource the caller may not read, before they are shaped.

    Used only by ``knowledge_follow_link``, which does its own link walk rather
    than going through :class:`GraphService`. That service now performs the same
    filtering itself; this remains here because the rows are built locally, and
    the rule has to be applied before the row carries the neighbour's name.

    A row carries that name, so emitting it with ``accessible: false`` is an
    existence-and-name oracle: it answers "does this exist and what is it called"
    for things the caller was never allowed to know about. The default is to omit
    the row entirely; the flag survives only as the explicit
    ``include_inaccessible`` opt-in.
    """
    if not rows:
        return []
    readable = {
        str(value)
        for value in PermissionService.allowed_resource_ids(
            ctx.user, [row["resource_id"] for row in rows], Permission.READ, api_key=ctx.api_key
        )
    }
    out: list[dict] = []
    for row in rows:
        if row["resource_id"] in readable:
            out.append({**row, "accessible": True})
        elif include_inaccessible:
            out.append({**row, "accessible": False})
    return out


def _normalize_folder(value) -> str:
    """Validate a caller-supplied folder path (rejects '..' and absolute paths)."""
    from apps.documents.folders import normalize_folder_path

    try:
        return normalize_folder_path(value)
    except ValidationError as exc:
        raise _service_error(exc) from exc


def _navigable_folders(ctx: ToolContext, workspace, project, folder: str = "") -> list[dict]:
    """Folders the caller may navigate: the children of ``folder`` plus its ancestors.

    Readability comes from :meth:`PermissionService.visible_resource_ids`, i.e.
    readable ∪ ancestors, so a grant on a deep folder stays reachable from the top
    of the tree while its siblings stay hidden - they are not readable, they are
    merely on the path to something that is.
    """
    from apps.documents.models import DocumentFolder

    folders = list(
        DocumentFolder.objects.select_related("resource").filter(
            workspace=workspace, project=project
        )
    )
    if not folders:
        return []
    resource_ids = [row.resource_id for row in folders]
    readable = {
        str(value)
        for value in PermissionService.allowed_resource_ids(
            ctx.user, resource_ids, Permission.READ, api_key=ctx.api_key
        )
    }
    visible = {
        str(value)
        for value in PermissionService.visible_resource_ids(
            ctx.user, resource_ids, Permission.READ, api_key=ctx.api_key
        )
    }
    prefix = f"{folder}/" if folder else ""
    rows = []
    for row in folders:
        path = row.path
        if path.startswith(prefix) and "/" not in path[len(prefix) :]:
            position = "child"
        elif folder and (folder == path or folder.startswith(f"{path}/")):
            position = "ancestor"
        else:
            continue
        if str(row.resource_id) not in visible:
            continue
        rows.append(
            {
                "path": path,
                "name": row.resource.name,
                "resource_id": str(row.resource_id),
                "readable": str(row.resource_id) in readable,
                "position": position,
            }
        )
    rows.sort(key=lambda item: item["path"])
    return rows


def _resolve_ownership_resource(args: dict) -> Resource:
    """The workspace/project resource the ownership tools act on."""
    if args.get("resource_id"):
        try:
            return Resource.objects.get(pk=args["resource_id"])
        except (Resource.DoesNotExist, ValidationError, ValueError, TypeError) as exc:
            raise ToolError("Resource not found.") from exc
    if args.get("project"):
        return _get_project(args["project"]).resource
    if args.get("workspace"):
        return _get_workspace(args["workspace"]).resource
    raise ToolError("Provide 'resource_id', or a 'workspace'/'project'.")


def _document_brief(document: Document) -> dict:
    return {
        "id": str(document.pk),
        "title": document.title,
        "path": document.path,
        "tree_path": document.tree_path(),
        "status": document.status,
        "priority": document.priority,
        "summary": document.summary,
        "workspace": str(document.workspace_id),
        "project": str(document.project_id) if document.project_id else None,
    }


def _folder_brief(folder) -> dict:
    return {
        "id": str(folder.pk),
        "name": folder.name,
        "path": folder.path,
        "tree_path": folder.tree_path(),
        "role": folder.role,
        "workspace": str(folder.workspace_id),
        "project": str(folder.project_id) if folder.project_id else None,
    }


def _accessible_documents(
    ctx, *, workspace=None, project=None, folder: str | None = None
) -> list[Document]:
    queryset = Document.objects.select_related("workspace", "project", "resource")
    if workspace is not None:
        queryset = queryset.filter(workspace=workspace)
    if project is not None:
        queryset = queryset.filter(project=project)
    if folder:
        queryset = queryset.filter(path__startswith=f"{folder}/")
    return [doc for doc in queryset if _may_read(ctx, doc.resource)]


# ---------------------------------------------------------------------------
# Knowledge read tools
# ---------------------------------------------------------------------------
@tool(
    "knowledge_search",
    "Search the knowledge base (hybrid full-text + semantic). Results are "
    "permission-filtered and ranked, preferring approved knowledge.",
    {
        "type": "object",
        "properties": {
            "query": {"type": "string"},
            "mode": {"type": "string", "enum": ["text", "semantic", "hybrid"]},
            "workspace": {"type": "string", "description": "Workspace id (optional)"},
            "project": {"type": "string", "description": "Project id (optional)"},
            "status": {"type": "string"},
            "limit": {"type": "integer", "minimum": 1, "maximum": 25},
        },
        "required": ["query"],
    },
)
def tool_search(ctx: ToolContext, args: dict) -> dict:
    query = str(args.get("query", ""))
    limit = min(int(args.get("limit", 10) or 10), 25)
    results = SearchService.search(
        ctx.user,
        query,
        mode=args.get("mode", "hybrid"),
        workspace_id=args.get("workspace"),
        project_id=args.get("project"),
        status=args.get("status"),
        limit=limit,
        api_key=ctx.api_key,
    )
    return {"query": query, "count": len(results), "results": results}


@tool(
    "knowledge_get",
    "Fetch a knowledge document including its content.",
    {
        "type": "object",
        "properties": {"document_id": {"type": "string"}},
        "required": ["document_id"],
    },
)
def tool_get(ctx: ToolContext, args: dict) -> dict:
    document = _get_document(args.get("document_id"))
    _require(ctx, document.resource, Permission.READ)
    payload = _document_brief(document)
    payload["content"] = DocumentService.read_content(document)
    payload["frontmatter"] = document.frontmatter
    return payload


@tool(
    "knowledge_get_summary",
    "Fetch only the title/summary. May be available without read access when the "
    "document is explicitly published (public_summary: true AND the workspace has "
    "publish_titles: true). Such a read is written to the audit log, so a "
    "published title is a deliberate disclosure, not a silent leak.",
    {
        "type": "object",
        "properties": {"document_id": {"type": "string"}},
        "required": ["document_id"],
    },
)
def tool_get_summary(ctx: ToolContext, args: dict) -> dict:
    document = _get_document(args.get("document_id"))
    if _may_read(ctx, document.resource):
        access = "full"
    elif publishes_summary(document.resource):
        # No read access, so the only thing being disclosed is the fact that a
        # document exists and what it is called - which the owner opted into.
        # Record it, or an un-audited existence oracle is just a leak.
        AuditService.log(
            AuditAction.READ,
            user=ctx.user,
            api_key=ctx.api_key,
            resource=document.resource,
            workspace=document.workspace,
            project=document.project,
            source=AuditSource.MCP,
            request=ctx.request,
            detail={"via": "public_summary", "type": "document_summary"},
        )
        access = "summary"
    else:
        raise ToolError("Permission denied.")
    return {
        "id": str(document.pk),
        "title": document.title,
        "summary": document.summary,
        "status": document.status,
        "access": access,
    }


@tool(
    "knowledge_update_workspace",
    "Rename a workspace, change its description, or change its slug (the URL key). "
    "Needs admin on it and is audited. A slug change breaks links shared under the "
    "old one - there is no redirect - but never moves the files: the on-disk layout "
    "keys on the workspace UUID.",
    {
        "type": "object",
        "properties": {
            "workspace": {"type": "string"},
            "name": {"type": "string"},
            "description": {"type": "string"},
            "slug": {"type": "string"},
        },
        "required": ["workspace"],
    },
)
def tool_update_workspace(ctx: ToolContext, args: dict) -> dict:
    from apps.workspaces.services import WorkspaceService

    workspace = _get_workspace(args.get("workspace"))
    _require(ctx, workspace.resource, Permission.ADMIN)
    previous_slug = workspace.slug
    try:
        WorkspaceService.update(
            workspace=workspace,
            name=args.get("name"),
            description=args.get("description"),
            slug=args.get("slug"),
            actor=ctx.user,
            request=ctx.request,
        )
    except (ValidationError, PermissionDenied) as exc:
        raise _service_error(exc) from exc
    return {
        "id": str(workspace.pk),
        "name": workspace.name,
        "slug": workspace.slug,
        "slug_changed": workspace.slug != previous_slug,
    }


@tool(
    "knowledge_list_workspaces",
    "List workspaces the caller can read. `is_personal` marks a Personal workspace: "
    "exactly one holder, never shareable, so it only ever shows up for its own "
    "owner. (Személyes workspace: kizárólag a tulajdonosa látja.)",
)
def tool_list_workspaces(ctx: ToolContext, args: dict) -> dict:
    workspaces = [
        {
            "id": str(workspace.pk),
            "name": workspace.name,
            "slug": workspace.slug,
            "kind": workspace.kind,
            "is_personal": workspace.is_personal,
            "owner": str(workspace.owner_id) if workspace.owner_id else None,
        }
        for workspace in Workspace.objects.select_related("resource")
        if _may_read(ctx, workspace.resource)
    ]
    return {"workspaces": workspaces}


@tool(
    "knowledge_my_workspace",
    "Return the caller's own Personal workspace, so an agent can find its private "
    "home without scanning the workspace list. Read-only: it never creates one. "
    "(Csak a saját személyes workspace-t adja vissza.)",
)
def tool_my_workspace(ctx: ToolContext, args: dict) -> dict:
    workspace = PersonalWorkspaceService.get_for(ctx.user)
    if workspace is None:
        return {"workspace": None}
    return {
        "workspace": {
            "id": str(workspace.pk),
            "name": workspace.name,
            "slug": workspace.slug,
            "kind": workspace.kind,
            "is_personal": True,
            "owner": str(workspace.owner_id) if workspace.owner_id else None,
        }
    }


@tool(
    "knowledge_list_projects",
    "List projects in a workspace the caller can read.",
    {"type": "object", "properties": {"workspace": {"type": "string"}}, "required": ["workspace"]},
)
def tool_list_projects(ctx: ToolContext, args: dict) -> dict:
    workspace = _get_workspace(args.get("workspace"))
    _require(ctx, workspace.resource, Permission.READ)
    projects = [
        {"id": str(project.pk), "name": project.name, "slug": project.slug}
        for project in workspace.project_set.select_related("resource")
        if _may_read(ctx, project.resource)
    ]
    return {"projects": projects}


@tool(
    "knowledge_list_documents",
    "List documents (optionally scoped to a workspace/project/folder). With a "
    "scope it also returns the folders the caller can navigate: the children of "
    "`folder` plus the path down to it, so a deep folder that was granted stays "
    "reachable from the top while its siblings stay hidden.",
    {
        "type": "object",
        "properties": {
            "workspace": {"type": "string"},
            "project": {"type": "string"},
            "folder": {
                "type": "string",
                "description": "Folder path inside the scope, e.g. 'skills/azure'. "
                "Omit for the top level.",
            },
        },
    },
)
def tool_list_documents(ctx: ToolContext, args: dict) -> dict:
    workspace = _get_workspace(args["workspace"]) if args.get("workspace") else None
    project = _get_project(args["project"]) if args.get("project") else None
    folder = _normalize_folder(args["folder"]) if args.get("folder") else ""
    documents = _accessible_documents(ctx, workspace=workspace, project=project, folder=folder or None)
    payload: dict = {"documents": [_document_brief(document) for document in documents]}
    if workspace is not None or project is not None:
        scope = workspace if workspace is not None else project.workspace
        payload["folder"] = folder
        payload["folders"] = _navigable_folders(ctx, scope, project, folder)
    return payload


TYPE_FOLDERS = {
    "skill": ["skills/"],
    "pattern": ["patterns/"],
    "convention": ["conventions/"],
    "decision": ["decisions/"],
    "example": ["examples/"],
}


def _matches_type(document: Document, type_name: str) -> bool:
    metadata = document.metadata or {}
    frontmatter = document.frontmatter or {}
    if metadata.get("type") == type_name or frontmatter.get("type") == type_name:
        return True
    path = (document.path or "").lower()
    return any(folder in path for folder in TYPE_FOLDERS.get(type_name, []))


def _make_type_tool(type_name: str):
    @tool(
        f"knowledge_get_{type_name}",
        f"List {type_name} knowledge (by metadata type or folder convention).",
        {
            "type": "object",
            "properties": {"workspace": {"type": "string"}, "project": {"type": "string"}},
        },
    )
    def _handler(ctx: ToolContext, args: dict) -> dict:
        workspace = _get_workspace(args["workspace"]) if args.get("workspace") else None
        project = _get_project(args["project"]) if args.get("project") else None
        documents = [
            _document_brief(document)
            for document in _accessible_documents(ctx, workspace=workspace, project=project)
            if _matches_type(document, type_name)
        ]
        return {type_name: documents, "count": len(documents)}

    return _handler


for _type in TYPE_FOLDERS:
    _make_type_tool(_type)


@tool(
    "knowledge_follow_link",
    "Traverse links from/to a resource (permission-checked). Use it to discover "
    "related knowledge across projects/workspaces. A neighbour the caller cannot "
    "read is omitted entirely - its name is not returned; pass "
    "`include_inaccessible` to get the row back with `accessible: false`, which "
    "does confirm that it exists.",
    {
        "type": "object",
        "properties": {
            "resource_id": {"type": "string"},
            "direction": {"type": "string", "enum": ["outgoing", "incoming"]},
            "include_inaccessible": {
                "type": "boolean",
                "description": "Return unreadable neighbours too, flagged "
                "`accessible: false`. Off by default: it leaks names.",
            },
        },
        "required": ["resource_id"],
    },
)
def tool_follow_link(ctx: ToolContext, args: dict) -> dict:
    try:
        resource = Resource.objects.get(pk=args.get("resource_id"))
    except (Resource.DoesNotExist, ValidationError, ValueError, TypeError) as exc:
        raise ToolError("Resource not found.") from exc
    _require(ctx, resource, Permission.READ)

    direction = args.get("direction", "outgoing")
    links = (
        resource.incoming_links.select_related("source")
        if direction == "incoming"
        else resource.outgoing_links.select_related("target")
    )
    rows = []
    for link in links:
        target = link.source if direction == "incoming" else link.target
        rows.append(
            {
                "resource_id": str(target.id),
                "name": target.name,
                "type": target.resource_type,
                "link_type": link.link_type,
                "summary_visible": publishes_summary(target),
            }
        )
    out = _readable_graph_rows(
        ctx, rows, include_inaccessible=bool(args.get("include_inaccessible"))
    )
    return {"links": out, "count": len(out)}


# ---------------------------------------------------------------------------
# Knowledge write tools
# ---------------------------------------------------------------------------
@tool(
    "knowledge_create_document",
    "Create a knowledge document (created as DRAFT by default).",
    {
        "type": "object",
        "properties": {
            "workspace": {"type": "string"},
            "project": {"type": "string"},
            "node": {
                "type": "string",
                "description": (
                    "Workspace-relative tree path of the document, e.g. "
                    "'Deploy/dotnet/x.md' - an alternative to project+path."
                ),
            },
            "title": {"type": "string"},
            "path": {"type": "string"},
            "content": {"type": "string"},
            "summary": {"type": "string"},
            "status": {"type": "string"},
        },
        "required": ["workspace", "title", "content"],
    },
)
def tool_create_document(ctx: ToolContext, args: dict) -> dict:
    workspace = _get_workspace(args["workspace"])
    project = _get_project(args["project"]) if args.get("project") else None
    # `node` is the additive address: the whole tree path, from which project and
    # scope path follow. `workspace` is still required so the path can be rooted.
    node = (args.get("node") or "").strip("/")
    path = args.get("path")
    if node:
        from apps.documents.folders import split_tree_path

        project, path = split_tree_path(workspace, node)
    target = project.resource if project else workspace.resource
    _require(ctx, target, Permission.WRITE)
    document = DocumentService.create(
        workspace=workspace,
        project=project,
        title=args.get("title", ""),
        path=path,
        content=args.get("content", ""),
        summary=args.get("summary", ""),
        status=args.get("status"),
        created_by=ctx.user,
        source=ChangeSource.MCP,
        request=ctx.request,
        api_key=ctx.api_key,
    )
    return _document_brief(document)


@tool(
    "knowledge_update_document",
    "Update a document (creates a new version).",
    {
        "type": "object",
        "properties": {
            "document_id": {"type": "string"},
            "content": {"type": "string"},
            "title": {"type": "string"},
            "summary": {"type": "string"},
            "status": {"type": "string"},
            "priority": {"type": "integer"},
        },
        "required": ["document_id"],
    },
)
def tool_update_document(ctx: ToolContext, args: dict) -> dict:
    document = _get_document(args["document_id"])
    _require(ctx, document.resource, Permission.WRITE)
    content = args.get("content")
    if content is None:
        content = DocumentService.read_content(document)
    updated = DocumentService.update_content(
        document=document,
        content=content,
        title=args.get("title"),
        summary=args.get("summary"),
        status=args.get("status"),
        priority=args.get("priority"),
        user=ctx.user,
        source=ChangeSource.MCP,
        request=ctx.request,
        api_key=ctx.api_key,
    )
    return _document_brief(updated)


@tool(
    "knowledge_delete_document",
    "Delete a document.",
    {
        "type": "object",
        "properties": {"document_id": {"type": "string"}},
        "required": ["document_id"],
    },
)
def tool_delete_document(ctx: ToolContext, args: dict) -> dict:
    document = _get_document(args["document_id"])
    _require(ctx, document.resource, Permission.DELETE)
    document_id = str(document.pk)
    DocumentService.delete(
        document=document, user=ctx.user, request=ctx.request, api_key=ctx.api_key
    )
    return {"deleted": document_id}


@tool(
    "knowledge_create_project",
    "Create a project inside a workspace.",
    {
        "type": "object",
        "properties": {
            "workspace": {"type": "string"},
            "name": {"type": "string"},
            "description": {"type": "string"},
        },
        "required": ["workspace", "name"],
    },
)
def tool_create_project(ctx: ToolContext, args: dict) -> dict:
    workspace = _get_workspace(args["workspace"])
    _require(ctx, workspace.resource, Permission.WRITE)
    project = ProjectService.create(
        workspace=workspace,
        name=args["name"],
        description=args.get("description", ""),
        created_by=ctx.user,
        request=ctx.request,
    )
    return {"id": str(project.pk), "name": project.name, "slug": project.slug}


@tool(
    "knowledge_update_project",
    "Update project name/description.",
    {
        "type": "object",
        "properties": {
            "project": {"type": "string"},
            "name": {"type": "string"},
            "description": {"type": "string"},
        },
        "required": ["project"],
    },
)
def tool_update_project(ctx: ToolContext, args: dict) -> dict:
    project = _get_project(args["project"])
    _require(ctx, project.resource, Permission.WRITE)
    if args.get("name"):
        project.name = args["name"]
        project.resource.name = args["name"]
        project.resource.save(update_fields=["name", "updated_at"])
    if args.get("description") is not None:
        project.description = args["description"]
    project.save()
    return {"id": str(project.pk), "name": project.name}


@tool(
    "knowledge_create_folder",
    "Create a folder node by its workspace-relative tree path, e.g. "
    "node='Deploy/runbooks'. Missing ancestors are created too.",
    {
        "type": "object",
        "properties": {
            "workspace": {"type": "string"},
            "node": {"type": "string", "description": "Workspace-relative tree path"},
            "description": {"type": "string"},
        },
        "required": ["workspace", "node"],
    },
)
def tool_create_folder(ctx: ToolContext, args: dict) -> dict:
    from apps.documents.folders import ensure_node_by_tree_path, folder_by_tree_path

    workspace = _get_workspace(args["workspace"])
    node = (args["node"] or "").strip("/")
    if not node:
        raise ToolError("A node (tree path) is required.")
    # Write is checked on the deepest existing ancestor (or the workspace), the
    # same rule the web folder page applies, so a grant on a subfolder is enough.
    segments = node.split("/")
    target = workspace.resource
    for cut in range(len(segments) - 1, 0, -1):
        ancestor = folder_by_tree_path(workspace, "/".join(segments[:cut]))
        if ancestor is not None:
            target = ancestor.resource
            break
    _require(ctx, target, Permission.WRITE)
    folder = ensure_node_by_tree_path(workspace, node, created_by=ctx.user)
    if args.get("description"):
        folder.description = args["description"]
        folder.save(update_fields=["description", "updated_at"])
    return _folder_brief(folder)


@tool(
    "knowledge_move_document",
    "Move a document into a folder node, addressed by its tree path "
    "(e.g. node='Deploy/dotnet').",
    {
        "type": "object",
        "properties": {
            "document_id": {"type": "string"},
            "node": {"type": "string", "description": "Destination folder tree path"},
        },
        "required": ["document_id", "node"],
    },
)
def tool_move_document(ctx: ToolContext, args: dict) -> dict:
    from apps.documents.folders import folder_by_tree_path, project_of_node

    document = _get_document(args["document_id"])
    _require(ctx, document.resource, Permission.WRITE)
    node = (args["node"] or "").strip("/")
    destination = folder_by_tree_path(document.workspace, node) if node else None
    if node and destination is None:
        raise ToolError(f"Folder '{node}' not found.")
    dest_project = project_of_node(destination)
    if (dest_project.pk if dest_project else None) != document.project_id:
        raise ToolError("A document can only move within its own project.")
    parent = destination.path if destination is not None else ""
    filename = (document.path or "").rsplit("/", 1)[-1]
    new_path = f"{parent}/{filename}" if parent else filename
    try:
        DocumentService.move(
            document, new_path, user=ctx.user, request=ctx.request, api_key=ctx.api_key
        )
    except ValidationError as exc:
        raise _service_error(exc) from exc
    return _document_brief(document)


@tool(
    "knowledge_move_folder",
    "Move a folder under another node, addressed by tree path; omit `node` to "
    "move it to its scope root.",
    {
        "type": "object",
        "properties": {
            "folder_id": {"type": "string"},
            "node": {
                "type": "string",
                "description": "Destination parent tree path; omit for the scope root",
            },
        },
        "required": ["folder_id"],
    },
)
def tool_move_folder(ctx: ToolContext, args: dict) -> dict:
    from apps.documents.folders import folder_by_tree_path, move_folder, project_of_node

    folder = _get_folder(args["folder_id"])
    _require(ctx, folder.resource, Permission.WRITE)
    node = (args.get("node") or "").strip("/")
    destination = folder_by_tree_path(folder.workspace, node) if node else None
    if node and destination is None:
        raise ToolError(f"Folder '{node}' not found.")
    # Only a given destination can cross a scope; "no node" means the folder's own
    # scope root, which is the same scope by construction.
    if node:
        dest_project = project_of_node(destination)
        if (dest_project.pk if dest_project else None) != folder.project_id:
            raise ToolError("A folder can only move within its own project.")
    try:
        move_folder(
            folder=folder,
            new_parent=destination,
            to_root=destination is None,
            user=ctx.user,
        )
    except ValidationError as exc:
        raise _service_error(exc) from exc
    return _folder_brief(folder)


# ---------------------------------------------------------------------------
# Ownership tools
# ---------------------------------------------------------------------------
_OWNERSHIP_SCOPE = {
    "resource_id": {"type": "string", "description": "Resource id (workspace or project)"},
    "workspace": {"type": "string", "description": "Workspace id or slug"},
    "project": {"type": "string", "description": "Project id"},
}


@tool(
    "knowledge_transfer_ownership",
    "Hand a workspace or a project to a new owner. Owner only. The new owner gets "
    "an ADMIN grant; the previous owner loses theirs unless `keep_access` keeps "
    "read/write. Ownership is not a shareable privilege, so this is deliberately "
    "not a grant, and a Personal workspace can never be transferred. Every "
    "transfer is written to the audit log. (Csak a tulajdonos adhatja tovább.)",
    {
        "type": "object",
        "properties": {
            **_OWNERSHIP_SCOPE,
            "new_owner": {"type": "string", "description": "Username or user id"},
            "keep_access": {
                "type": "string",
                "enum": ["read", "write"],
                "description": "What the previous owner keeps. Omit to revoke "
                "their admin grant; it can never be admin.",
            },
        },
        "required": ["new_owner"],
    },
)
def tool_transfer_ownership(ctx: ToolContext, args: dict) -> dict:
    resource = _resolve_ownership_resource(args)
    _require(ctx, resource, Permission.ADMIN)
    new_owner = _get_user(args.get("new_owner"))
    keep_access = args.get("keep_access") or None
    if keep_access not in (None, Permission.READ, Permission.WRITE):
        raise ToolError("'keep_access' must be 'read' or 'write'.")
    previous_owner_id = PermissionService.scope_owner_id(resource)
    try:
        obj = OwnershipService.transfer(
            resource=resource,
            new_owner=new_owner,
            actor=ctx.user,
            keep_old_access=keep_access,
            request=ctx.request,
        )
    except (PermissionDenied, ValidationError) as exc:
        raise _service_error(exc) from exc
    return {
        "transferred": True,
        "resource_id": str(resource.id),
        "owner": str(obj.owner_id),
        "previous_owner": str(previous_owner_id) if previous_owner_id else None,
        "keep_access": keep_access,
    }


@tool(
    "knowledge_take_over",
    "AUDITED break-glass, SUPERUSER ONLY: write yourself an ADMIN entry on a "
    "resource so you can act on it. This does NOT change the owner and it is "
    "blocked on any resource whose takeover flag is set (or inherits one) - it is "
    "a way in, never a way to lock somebody out. Every call is written to the "
    "audit log as a permission change. "
    "(Csak superuser; minden hívás naplózva.)",
    {"type": "object", "properties": dict(_OWNERSHIP_SCOPE)},
)
def tool_take_over(ctx: ToolContext, args: dict) -> dict:
    if not getattr(ctx.user, "is_superuser", False):
        raise ToolError("Superuser access required to take over a resource.")
    resource = _resolve_ownership_resource(args)
    try:
        entry = OwnershipService.take_over(
            resource=resource, actor=ctx.user, request=ctx.request
        )
    except PermissionDenied as exc:
        raise _service_error(exc) from exc
    return {
        "taken_over": True,
        "audited": True,
        "resource_id": str(resource.id),
        "resource_name": resource.name,
        "entry": {
            "id": str(entry.pk),
            "subject_id": str(entry.subject_id),
            "permission": entry.permission,
            "effect": entry.effect,
            "inherit": entry.inherit,
        },
    }


# ---------------------------------------------------------------------------
# Git tools
# ---------------------------------------------------------------------------
@tool(
    "knowledge_get_git_status",
    "Get Git status of a repository attached to a workspace/project.",
    {
        "type": "object",
        "properties": {
            "workspace": {"type": "string"},
            "project": {"type": "string"},
            "repository": {"type": "string"},
        },
    },
)
def tool_git_status(ctx: ToolContext, args: dict) -> dict:
    repository = _resolve_repository_arg(ctx, args)
    _require(ctx, repository.resource, Permission.READ)
    return GitService.status_repository(repository)


@tool(
    "knowledge_create_branch",
    "Create (and check out) a branch in a Git-backed repository.",
    {
        "type": "object",
        "properties": {"repository": {"type": "string"}, "name": {"type": "string"}},
        "required": ["repository", "name"],
    },
)
def tool_git_branch(ctx: ToolContext, args: dict) -> dict:
    repository = _get_repository(args["repository"])
    _require(ctx, repository.resource, Permission.WRITE)
    client = GitService._client(repository)
    if client.current_branch() == args["name"]:
        return {"branch": args["name"], "branches": client.branches()}
    try:
        if args["name"] in client.branches():
            client.checkout(args["name"])
        else:
            client.checkout_new(args["name"])
    except GitError as exc:
        raise ToolError(str(exc)) from exc
    return {"branch": args["name"], "branches": client.branches()}


@tool(
    "knowledge_create_commit",
    "Commit working tree changes in a Git-backed repository.",
    {
        "type": "object",
        "properties": {"repository": {"type": "string"}, "message": {"type": "string"}},
        "required": ["repository", "message"],
    },
)
def tool_git_commit(ctx: ToolContext, args: dict) -> dict:
    repository = _get_repository(args["repository"])
    _require(ctx, repository.resource, Permission.WRITE)
    try:
        sha = GitService.commit_repository(
            repository=repository, message=args["message"], user=ctx.user, request=ctx.request
        )
    except GitError as exc:
        raise ToolError(str(exc)) from exc
    return {"sha": sha, "committed": bool(sha)}


@tool(
    "knowledge_create_pull_request",
    "Open a GitHub pull request from a branch of a Git-backed repository.",
    {
        "type": "object",
        "properties": {
            "repository": {"type": "string"},
            "title": {"type": "string"},
            "body": {"type": "string"},
            "head": {"type": "string"},
            "base": {"type": "string"},
        },
        "required": ["repository", "title"],
    },
)
def tool_git_pull_request(ctx: ToolContext, args: dict) -> dict:
    repository = _get_repository(args["repository"])
    _require(ctx, repository.resource, Permission.WRITE)
    if not settings.BRAINBOX_GITHUB_TOKEN:
        raise ToolError("BRAINBOX_GITHUB_TOKEN is not configured; cannot open a PR.")
    client = GitService._client(repository)
    head = args.get("head") or client.current_branch()
    base = args.get("base") or repository.default_branch
    url = _github_pull_request(
        repository.remote_url,
        settings.BRAINBOX_GITHUB_TOKEN,
        args["title"],
        args.get("body", ""),
        head,
        base,
    )
    return {"pull_request": url, "head": head, "base": base}


def _github_pull_request(remote_url, token, title, body, head, base) -> str:
    match = re.match(r"https://github\.com/([^/]+)/([^/]+?)(?:\.git)?$", remote_url or "")
    if not match:
        raise ToolError("Pull requests are only supported for github.com remotes.")
    owner, repo = match.group(1), match.group(2)
    payload = json.dumps({"title": title, "body": body, "head": head, "base": base}).encode()
    request = urllib.request.Request(
        f"https://api.github.com/repos/{owner}/{repo}/pulls",
        data=payload,
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "Content-Type": "application/json",
            "User-Agent": "brainbox",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            data = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        raise ToolError(f"GitHub PR failed: {exc.read().decode(errors='replace')[:300]}") from exc
    return data.get("html_url", "")


def _resolve_repository_arg(ctx, args) -> GitRepository:
    if args.get("repository"):
        return _get_repository(args["repository"])
    workspace = _get_workspace(args["workspace"]) if args.get("workspace") else None
    project = _get_project(args["project"]) if args.get("project") else None
    if workspace is None:
        raise ToolError("Provide 'repository', or a 'workspace'/'project'.")
    repository = GitService.repository_for(workspace=workspace, project=project)
    if repository is None:
        raise ToolError("No Git repository attached.")
    return repository


# ---------------------------------------------------------------------------
# Secret tools
# ---------------------------------------------------------------------------
@tool("secret_list", "List your secrets (metadata only, never values).")
def tool_secret_list(ctx: ToolContext, args: dict) -> dict:
    secrets = [
        SecretService.metadata(secret) for secret in Secret.objects.filter(owner=ctx.user)
    ]
    return {"secrets": secrets}


@tool(
    "secret_get_metadata",
    "Get metadata/scope of one of your secrets (no value).",
    {
        "type": "object",
        "properties": {"secret_id": {"type": "string"}},
        "required": ["secret_id"],
    },
)
def tool_secret_metadata(ctx: ToolContext, args: dict) -> dict:
    secret = _get_secret(ctx, args["secret_id"])
    return SecretService.metadata(secret)


@tool(
    "secret_use",
    "Use a secret in a workspace/project context. Returns the credential for "
    "injection; the use is audited.",
    {
        "type": "object",
        "properties": {
            "secret_id": {"type": "string"},
            "workspace": {"type": "string"},
            "project": {"type": "string"},
        },
        "required": ["secret_id"],
    },
)
def tool_secret_use(ctx: ToolContext, args: dict) -> dict:
    secret = _get_secret(ctx, args["secret_id"])
    workspace_id = args.get("workspace")
    if not workspace_id and args.get("project"):
        try:
            workspace_id = str(_get_project(args["project"]).workspace_id)
        except ToolError:
            workspace_id = None
    try:
        value = SecretService.reveal(
            secret,
            user=ctx.user,
            workspace_id=workspace_id,
            project_id=args.get("project"),
            request=ctx.request,
            api_key=ctx.api_key,
        )
    except Exception as exc:  # noqa: BLE001 - PermissionDenied etc.
        raise ToolError(str(exc)) from exc
    return {"secret_id": str(secret.pk), "name": secret.name, "value": value}


def _get_secret(ctx: ToolContext, secret_id) -> Secret:
    try:
        secret = Secret.objects.get(pk=secret_id)
    except (Secret.DoesNotExist, ValueError, TypeError) as exc:
        raise ToolError("Secret not found.") from exc
    if secret.owner_id != getattr(ctx.user, "id", None):
        raise ToolError("Secret not found.")
    return secret


# ---------------------------------------------------------------------------
# Deadlines / agenda (Phase: naptár + agenda)
# ---------------------------------------------------------------------------
@tool(
    "knowledge_deadlines",
    "Get the upcoming agenda: deadlines extracted from knowledge files "
    "(tech-debt revisits, review dates, targets). Use days_ahead=0 for today's "
    "agenda, or omit it for a 30-day outlook. Sorted by due date, overdue first.",
    {
        "type": "object",
        "properties": {
            "days_ahead": {
                "type": "integer",
                "minimum": 0,
                "maximum": 365,
                "description": "0 = today only, 7 = this week. Default 30.",
            },
            "workspace": {"type": "string"},
            "project": {"type": "string"},
            "include_done": {"type": "boolean"},
        },
    },
)
def tool_deadlines(ctx: ToolContext, args: dict) -> dict:
    from datetime import date, timedelta

    from apps.deadlines.models import DeadlineStatus, KnowledgeDeadline

    days_ahead = max(0, int(args.get("days_ahead", 30) or 0))
    today = date.today()
    until = today + timedelta(days=days_ahead)

    deadlines = KnowledgeDeadline.objects.filter(
        due_date__lte=until, status=DeadlineStatus.OPEN
    ).select_related("document", "workspace", "project", "resource")

    if not args.get("include_done"):
        deadlines = deadlines.exclude(status__in=[DeadlineStatus.DONE, DeadlineStatus.DISMISSED])
    if args.get("workspace"):
        deadlines = deadlines.filter(workspace_id=args["workspace"])
    if args.get("project"):
        deadlines = deadlines.filter(project_id=args["project"])

    items = []
    for deadline in deadlines:
        if not _may_read(ctx, deadline.resource):
            continue  # permission-aware: never leak an inaccessible deadline
        items.append(
            {
                "id": str(deadline.id),
                "title": deadline.title,
                "due_date": deadline.due_date.isoformat(),
                "overdue": deadline.due_date < today,
                "days_until": (deadline.due_date - today).days,
                "status": deadline.status,
                "document_id": str(deadline.document_id),
                "document_title": deadline.document.title,
                "workspace": str(deadline.workspace_id),
                "project": str(deadline.project_id) if deadline.project_id else None,
            }
        )
    items.sort(key=lambda row: row["due_date"])
    return {"today": today.isoformat(), "days_ahead": days_ahead, "count": len(items), "deadlines": items}


# ---------------------------------------------------------------------------
# Runtime settings (UI-managed, superuser)
# ---------------------------------------------------------------------------
@tool(
    "knowledge_get_settings",
    "Read the current runtime configuration (AI/embedding, search, git, jobs). "
    "Secret values are masked. Use this to check which model/provider is active. "
    "Staff only: the key names alone describe the deployment. "
    "(Csak staff tag láthatja.)",
    {"type": "object", "properties": {"category": {"type": "string"}}},
)
def tool_get_settings(ctx: ToolContext, args: dict) -> dict:
    from apps.settings_store.services import describe

    # Masked, not harmless: the key names and the active model/provider are still
    # a map of the deployment, so this is staff-only rather than "any caller".
    if not getattr(ctx.user, "is_staff", False):
        raise ToolError("Staff access required to read the settings.")
    rows = describe()
    category = args.get("category")
    if category:
        rows = [row for row in rows if row["category"].lower() == str(category).lower()]
    return {
        "count": len(rows),
        "settings": {
            row["key"]: row["current"] for row in rows
        },
        "overridden": [row["key"] for row in rows if row["overridden"]],
    }


@tool(
    "knowledge_set_setting",
    "Override a runtime setting (superuser only). Secrets are accepted but never echoed back.",
    {
        "type": "object",
        "properties": {
            "key": {"type": "string", "description": "e.g. BRAINBOX_LLM_MODEL"},
            "value": {"type": "string"},
        },
        "required": ["key", "value"],
    },
)
def tool_set_setting(ctx: ToolContext, args: dict) -> dict:
    from apps.settings_store.services import describe, set_value

    if not getattr(ctx.user, "is_superuser", False):
        raise ToolError("Superuser access required to change settings.")
    key = str(args.get("key", ""))
    if key not in {row["key"] for row in describe()}:
        raise ToolError(f"Unknown setting '{key}'.")
    try:
        set_value(key=key, raw=args.get("value", ""), user=ctx.user)
    except ValidationError as exc:
        raise ToolError("; ".join(exc.messages)) from exc
    return {"key": key, "saved": True}


# ---------------------------------------------------------------------------
# Knowledge intelligence (Phase 7)
# ---------------------------------------------------------------------------
@tool(
    "knowledge_discover",
    "Discover skills/patterns/conventions/decisions/examples relevant to a workspace.",
    {
        "type": "object",
        "properties": {
            "workspace": {"type": "string"},
            "types": {
                "type": "array",
                "items": {"type": "string"},
                "description": "e.g. [skill, pattern, convention, decision, example]",
            },
        },
    },
)
def tool_discover(ctx: ToolContext, args: dict) -> dict:
    return DiscoveryService.discover(
        ctx.user,
        api_key=ctx.api_key,
        types=args.get("types"),
        workspace_id=args.get("workspace"),
        limit=25,
    )


@tool(
    "knowledge_list_templates",
    "List reusable document templates (markdown skeletons for deploy types, "
    "runbooks, review notes...).",
    {"type": "object", "properties": {"workspace": {"type": "string"}, "project": {"type": "string"}}},
)
def tool_list_templates(ctx: ToolContext, args: dict) -> dict:
    templates = [
        doc
        for doc in Document.objects.filter(
            is_template=True,
            **({"workspace_id": args["workspace"]} if args.get("workspace") else {}),
            **({"project_id": args["project"]} if args.get("project") else {}),
        )
        if _may_read(ctx, doc.resource)
    ]
    return {
        "templates": [
            {
                "document_id": str(doc.pk),
                "title": doc.title,
                "path": doc.path,
                "workspace": str(doc.workspace_id),
                "project": str(doc.project_id) if doc.project_id else None,
            }
            for doc in templates
        ]
    }


@tool(
    "knowledge_create_from_template",
    "Create a new document from a template (placeholders {{title}}/{{date}} are filled).",
    {
        "type": "object",
        "properties": {
            "template_id": {"type": "string"},
            "workspace": {"type": "string"},
            "project": {"type": "string"},
            "title": {"type": "string"},
            "path": {"type": "string"},
        },
        "required": ["template_id", "workspace"],
    },
)
def tool_create_from_template(ctx: ToolContext, args: dict) -> dict:
    from apps.documents.services import DocumentService

    template = _get_document(args["template_id"])
    if not template.is_template:
        raise ToolError(f"Document '{template.pk}' is not a template.")
    workspace = _get_workspace(args["workspace"])
    project = _get_project(args["project"]) if args.get("project") else None
    target = project.resource if project else workspace.resource
    _require(ctx, target, Permission.WRITE)
    document = DocumentService.instantiate_template(
        template=template,
        workspace=workspace,
        project=project,
        title=args.get("title", ""),
        path=args.get("path", ""),
        created_by=ctx.user,
        request=ctx.request,
        api_key=ctx.api_key,
    )
    return _document_brief(document)


@tool(
    "knowledge_get_related",
    "Get related knowledge via the link graph around a resource (permission-filtered). "
    "A neighbour the caller cannot read is omitted entirely - its name is not "
    "returned; pass `include_inaccessible` to get the row back with "
    "`accessible: false`, which does confirm that it exists.",
    {
        "type": "object",
        "properties": {
            "resource_id": {"type": "string"},
            "depth": {"type": "integer", "minimum": 1, "maximum": 3},
            "include_inaccessible": {
                "type": "boolean",
                "description": "Return unreadable neighbours too, flagged "
                "`accessible: false`. Off by default: it leaks names.",
            },
        },
        "required": ["resource_id"],
    },
)
def tool_related(ctx: ToolContext, args: dict) -> dict:
    try:
        resource = Resource.objects.get(pk=args["resource_id"])
    except (Resource.DoesNotExist, ValidationError, ValueError, TypeError) as exc:
        raise ToolError("Resource not found.") from exc
    _require(ctx, resource, Permission.READ)
    # The unreadable-neighbour filtering now lives in GraphService itself (it is
    # the one implementation the web UI and the REST API share), so this tool
    # no longer has to drop rows itself - filtering in one of three callers is
    # how the surfaces used to disagree about who can see what.
    results = GraphService.neighbors(
        ctx.user,
        resource,
        api_key=ctx.api_key,
        depth=int(args.get("depth", 1)),
        include_inaccessible=bool(args.get("include_inaccessible")),
    )
    return {"related": results, "count": len(results)}


@tool(
    "knowledge_generate_draft",
    "Generate an AI draft document (always DRAFT) using company knowledge as context.",
    {
        "type": "object",
        "properties": {
            "workspace": {"type": "string"},
            "project": {"type": "string"},
            "title": {"type": "string"},
            "prompt": {"type": "string"},
        },
        "required": ["workspace", "prompt"],
    },
)
def tool_generate_draft(ctx: ToolContext, args: dict) -> dict:
    workspace = _get_workspace(args["workspace"])
    project = _get_project(args["project"]) if args.get("project") else None
    target = project.resource if project else workspace.resource
    _require(ctx, target, Permission.WRITE)
    document = DraftService.generate_draft(
        workspace=workspace,
        project=project,
        title=args.get("title") or "Untitled draft",
        prompt=args["prompt"],
        user=ctx.user,
        request=ctx.request,
    )
    return _document_brief(document)


@tool(
    "knowledge_approve_document",
    "Approve a draft document (human review workflow).",
    {
        "type": "object",
        "properties": {"document_id": {"type": "string"}},
        "required": ["document_id"],
    },
)
def tool_approve(ctx: ToolContext, args: dict) -> dict:
    document = _get_document(args["document_id"])
    _require(ctx, document.resource, Permission.WRITE)
    updated = DraftService.set_status(
        document=document,
        status=DocumentStatus.APPROVED,
        user=ctx.user,
        request=ctx.request,
        api_key=ctx.api_key,
    )
    return _document_brief(updated)


@tool(
    "knowledge_quality_metrics",
    "Knowledge quality metrics (superuser only): status mix, orphans, stale docs. "
    "Counts every workspace, including other people's. "
    "(The REST QualityView uses the same superuser-only gate.)",
)
def tool_quality(ctx: ToolContext, args: dict) -> dict:
    # Must stay in step with the REST QualityView: the numbers are global
    # (every workspace, other people's included), so staff is not sufficient.
    if not getattr(ctx.user, "is_superuser", False):
        raise ToolError("Superuser access required.")
    return QualityService.metrics()


# ---------------------------------------------------------------------------
# Egress gateway
# ---------------------------------------------------------------------------
@tool(
    "gateway_list",
    "List the external targets (APIs, MCP servers) you may reach through Brainbox. "
    "The credentials for them live in the vault, not in your configuration.",
    {"type": "object", "properties": {}},
)
def tool_gateway_list(ctx: ToolContext, args: dict) -> dict:
    targets = GatewayService.visible(ctx.user, api_key=ctx.api_key)
    return {
        "targets": [
            {
                "id": str(target.pk),
                "name": target.name,
                "kind": target.kind,
                "base_url": target.base_url,
                "workspace": str(target.workspace_id) if target.workspace_id else None,
                "project": str(target.project_id) if target.project_id else None,
            }
            for target in targets
        ]
    }


@tool(
    "gateway_call",
    "Call an external target through Brainbox. The credential is injected "
    "server-side and never returned.",
    {
        "type": "object",
        "properties": {
            "target": {"type": "string", "description": "Target id or name"},
            "method": {
                "type": "string",
                "enum": ["GET", "POST", "PUT", "PATCH", "DELETE"],
            },
            "path": {"type": "string", "description": "Appended to the target base URL"},
            "body": {"type": "object"},
            "headers": {"type": "object"},
        },
        "required": ["target"],
    },
)
def tool_gateway_call(ctx: ToolContext, args: dict) -> dict:
    target = GatewayService.resolve(args["target"])
    try:
        return GatewayService.call(
            target,
            method=args.get("method", "GET"),
            path=args.get("path", ""),
            body=args.get("body"),
            headers=args.get("headers"),
            user=ctx.user,
            request=ctx.request,
            api_key=ctx.api_key,
            source=AuditSource.MCP,
        )
    except (PermissionDenied, ValidationError, RateLimited) as exc:
        raise _service_error(exc) from exc


@tool(
    "gateway_mcp",
    "Call a tool on an external MCP server (HTTP JSON-RPC) through Brainbox, or "
    "list its tools with list_tools=true.",
    {
        "type": "object",
        "properties": {
            "target": {"type": "string", "description": "MCP target id or name"},
            "tool": {"type": "string", "description": "Remote tool name"},
            "arguments": {"type": "object"},
            "list_tools": {"type": "boolean"},
        },
        "required": ["target"],
    },
)
def tool_gateway_mcp(ctx: ToolContext, args: dict) -> dict:
    target = GatewayService.resolve(args["target"])
    if target.kind != GatewayKind.MCP:
        raise ToolError("Ez a cél nem MCP szerver.")
    path = target.config.get("mcp_path") or "/mcp"
    if args.get("list_tools"):
        rpc = {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}}
    else:
        if not args.get("tool"):
            raise ToolError("A 'tool' mező kötelező.")
        rpc = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": args["tool"], "arguments": args.get("arguments") or {}},
        }
    try:
        result = GatewayService.call(
            target,
            method="POST",
            path=path,
            body=rpc,
            # Streamable HTTP: a server may answer with JSON or an SSE stream.
            headers={"Accept": "application/json, text/event-stream"},
            user=ctx.user,
            request=ctx.request,
            api_key=ctx.api_key,
            source=AuditSource.MCP,
        )
    except (PermissionDenied, ValidationError, RateLimited) as exc:
        raise _service_error(exc) from exc
    content_type = next(
        (
            value
            for key, value in (result.get("headers") or {}).items()
            if key.lower() == "content-type"
        ),
        "",
    )
    if "text/event-stream" in content_type.lower():
        messages = parse_sse_json(result["body"])
        for message in messages:
            if isinstance(message, dict) and message.get("id") is not None:
                return message
        return {"messages": messages}
    # The remote answers with a JSON-RPC envelope; surface it verbatim when it is
    # JSON, otherwise hand back the raw status/body.
    try:
        return json.loads(result["body"])
    except (ValueError, TypeError):
        return result


def _gateway_brief(target) -> dict:
    return {
        "id": str(target.pk),
        "name": target.name,
        "kind": target.kind,
        "base_url": target.base_url,
        "enabled": target.enabled,
        "config": target.config,
        "workspace": str(target.workspace_id) if target.workspace_id else None,
        "project": str(target.project_id) if target.project_id else None,
    }


def _owned_secret(ctx: ToolContext, value):
    """Resolve one of the caller's own active secrets, or None/raise.

    A target's credential is a ``Secret``: it lives outside every workspace and
    is reachable only through its owner, so an agent may only point a target at
    a secret it owns - never at someone else's.
    """
    if not value:
        return None
    try:
        return Secret.objects.get(pk=value, owner=ctx.user, is_active=True)
    except (Secret.DoesNotExist, ValidationError, ValueError, TypeError):
        raise ToolError(f"Secret '{value}' not found (must be one of yours).") from None


@tool(
    "knowledge_create_gateway_target",
    "Create an egress target: an external API / MCP server reached through Brainbox. "
    "The credential stays in the vault; the caller only names a target. Needs write "
    "on the target workspace, or no workspace for a personal target.",
    {
        "type": "object",
        "properties": {
            "name": {"type": "string"},
            "base_url": {"type": "string"},
            "kind": {"type": "string", "enum": ["http", "mcp", "github"]},
            "workspace": {"type": "string", "description": "Workspace id or slug (optional)"},
            "project": {"type": "string", "description": "Project id (optional)"},
            "secret_id": {"type": "string", "description": "One of your own secrets (optional)"},
            "config": {"type": "object"},
        },
        "required": ["name", "base_url"],
    },
)
def tool_create_gateway_target(ctx: ToolContext, args: dict) -> dict:
    workspace = _get_workspace(args["workspace"]) if args.get("workspace") else None
    project = _get_project(args["project"]) if args.get("project") else None
    if project is not None and workspace is None:
        workspace = project.workspace
    if project is not None:
        _require(ctx, project.resource, Permission.WRITE)
    elif workspace is not None:
        _require(ctx, workspace.resource, Permission.WRITE)
    secret = _owned_secret(ctx, args.get("secret_id"))
    try:
        target = GatewayService.create(
            name=args["name"],
            base_url=args["base_url"],
            kind=args.get("kind", "http"),
            config=args.get("config") or {},
            secret=secret,
            workspace=workspace,
            project=project,
            created_by=ctx.user,
            request=ctx.request,
        )
    except ValidationError as exc:
        raise _service_error(exc) from exc
    return _gateway_brief(target)


@tool(
    "knowledge_update_gateway_target",
    "Update an egress target (name, base_url, kind, config, enabled, secret).",
    {
        "type": "object",
        "properties": {
            "target": {"type": "string", "description": "Target id or name"},
            "name": {"type": "string"},
            "base_url": {"type": "string"},
            "kind": {"type": "string", "enum": ["http", "mcp", "github"]},
            "config": {"type": "object"},
            "enabled": {"type": "boolean"},
            "secret_id": {
                "type": ["string", "null"],
                "description": "Your secret id, or null to clear the credential",
            },
        },
        "required": ["target"],
    },
)
def tool_update_gateway_target(ctx: ToolContext, args: dict) -> dict:
    target = GatewayService.resolve(args["target"])
    _require(ctx, target.resource, Permission.WRITE)
    secret = UNSET
    if "secret_id" in args:
        secret = _owned_secret(ctx, args.get("secret_id"))
    try:
        GatewayService.update(
            target,
            name=args.get("name"),
            base_url=args.get("base_url"),
            kind=args.get("kind"),
            config=args.get("config"),
            enabled=args.get("enabled"),
            secret=secret,
            created_by=ctx.user,
            request=ctx.request,
        )
    except ValidationError as exc:
        raise _service_error(exc) from exc
    return _gateway_brief(target)


@tool(
    "knowledge_delete_gateway_target",
    "Delete an egress target.",
    {
        "type": "object",
        "properties": {"target": {"type": "string"}},
        "required": ["target"],
    },
)
def tool_delete_gateway_target(ctx: ToolContext, args: dict) -> dict:
    target = GatewayService.resolve(args["target"])
    _require(ctx, target.resource, Permission.DELETE)
    name = target.name
    GatewayService.delete(target, user=ctx.user, request=ctx.request)
    return {"deleted": name}


# ---------------------------------------------------------------------------
# Curator
# ---------------------------------------------------------------------------
def _proposal_brief(proposal) -> dict:
    return {
        "id": str(proposal.pk),
        "kind": proposal.kind,
        "status": proposal.status,
        "title": proposal.title,
        "rationale": proposal.rationale,
        "workspace": str(proposal.workspace_id),
        "created_at": proposal.created_at.isoformat() if proposal.created_at else None,
    }


def _get_proposal(value):
    from apps.curator.models import CuratorProposal

    try:
        return CuratorProposal.objects.select_related("workspace", "resource").get(pk=value)
    except (CuratorProposal.DoesNotExist, ValidationError, ValueError, TypeError):
        raise ToolError(f"Proposal '{value}' not found.") from None


@tool(
    "curator_list_proposals",
    "List the curator's tidy-up proposals (duplicates, stale pages). Defaults to the "
    "open review queue; pass status to see decided ones.",
    {
        "type": "object",
        "properties": {
            "workspace": {"type": "string", "description": "Workspace id or slug (optional)"},
            "status": {"type": "string", "enum": ["open", "applied", "rejected", "stale"]},
        },
    },
)
def tool_curator_list_proposals(ctx: ToolContext, args: dict) -> dict:
    from apps.curator.models import CuratorProposal, CuratorStatus

    queryset = CuratorProposal.objects.select_related("workspace", "resource")
    if args.get("workspace"):
        workspace = _get_workspace(args["workspace"])
        _require(ctx, workspace.resource, Permission.READ)
        queryset = queryset.filter(workspace=workspace)
    queryset = queryset.filter(status=args.get("status") or CuratorStatus.OPEN)
    proposals = [
        _proposal_brief(proposal)
        for proposal in queryset
        if PermissionService.check(
            ctx.user,
            proposal.resource or proposal.workspace.resource,
            Permission.READ,
            api_key=ctx.api_key,
        )
    ]
    return {"count": len(proposals), "proposals": proposals}


@tool(
    "curator_scan",
    "Run the curator over a workspace and create proposals for any drift found.",
    {
        "type": "object",
        "properties": {"workspace": {"type": "string"}},
        "required": ["workspace"],
    },
)
def tool_curator_scan(ctx: ToolContext, args: dict) -> dict:
    from apps.curator.services import CuratorService

    workspace = _get_workspace(args["workspace"])
    _require(ctx, workspace.resource, Permission.WRITE)
    return CuratorService.scan_workspace(workspace)


def _curator_decide(ctx: ToolContext, args: dict, *, approve: bool) -> dict:
    from apps.curator.services import CuratorService

    proposal = _get_proposal(args["proposal_id"])
    if not CuratorService.can_decide(ctx.user, proposal, api_key=ctx.api_key):
        raise ToolError("Nincs jogosultságod elbírálni ezt a javaslatot.")
    try:
        CuratorService.decide(
            proposal,
            approve=approve,
            user=ctx.user,
            request=ctx.request,
            api_key=ctx.api_key,
        )
    except ValidationError as exc:
        raise _service_error(exc) from exc
    return _proposal_brief(proposal)


@tool(
    "curator_approve",
    "Approve a curator proposal: apply its change (through the normal services).",
    {
        "type": "object",
        "properties": {"proposal_id": {"type": "string"}},
        "required": ["proposal_id"],
    },
)
def tool_curator_approve(ctx: ToolContext, args: dict) -> dict:
    return _curator_decide(ctx, args, approve=True)


@tool(
    "curator_reject",
    "Reject a curator proposal: leave the knowledge unchanged.",
    {
        "type": "object",
        "properties": {"proposal_id": {"type": "string"}},
        "required": ["proposal_id"],
    },
)
def tool_curator_reject(ctx: ToolContext, args: dict) -> dict:
    return _curator_decide(ctx, args, approve=False)


# ---------------------------------------------------------------------------
# Comments
# ---------------------------------------------------------------------------
def _comment_brief(comment) -> dict:
    return {
        "id": str(comment.pk),
        "document": str(comment.document_id),
        "author": comment.author.username if comment.author_id else None,
        "body": comment.body,
        "resolved": comment.resolved,
        "created_at": comment.created_at.isoformat() if comment.created_at else None,
    }


@tool(
    "document_comments",
    "List the comments on a document.",
    {
        "type": "object",
        "properties": {"document_id": {"type": "string"}},
        "required": ["document_id"],
    },
)
def tool_document_comments(ctx: ToolContext, args: dict) -> dict:
    from apps.documents.comments import CommentService

    document = _get_document(args["document_id"])
    comments = CommentService.list_for(document, ctx.user, api_key=ctx.api_key)
    return {"comments": [_comment_brief(comment) for comment in comments]}


@tool(
    "document_comment_add",
    "Add a comment to a document.",
    {
        "type": "object",
        "properties": {
            "document_id": {"type": "string"},
            "body": {"type": "string"},
        },
        "required": ["document_id", "body"],
    },
)
def tool_document_comment_add(ctx: ToolContext, args: dict) -> dict:
    from apps.documents.comments import CommentService

    document = _get_document(args["document_id"])
    try:
        comment = CommentService.add(
            document=document,
            author=ctx.user,
            body=args.get("body", ""),
            request=ctx.request,
            api_key=ctx.api_key,
        )
    except (PermissionDenied, ValidationError) as exc:
        raise _service_error(exc) from exc
    return _comment_brief(comment)


@tool(
    "document_comment_resolve",
    "Resolve a comment, or reopen it with resolved=false.",
    {
        "type": "object",
        "properties": {
            "comment_id": {"type": "string"},
            "resolved": {"type": "boolean"},
        },
        "required": ["comment_id"],
    },
)
def tool_document_comment_resolve(ctx: ToolContext, args: dict) -> dict:
    from apps.documents.comments import CommentService
    from apps.documents.models import DocumentComment

    try:
        comment = DocumentComment.objects.select_related("document", "author").get(
            pk=args["comment_id"]
        )
    except (DocumentComment.DoesNotExist, ValidationError, ValueError, TypeError):
        raise ToolError(f"Comment '{args['comment_id']}' not found.") from None
    try:
        CommentService.set_resolved(
            comment=comment,
            user=ctx.user,
            resolved=bool(args.get("resolved", True)),
            request=ctx.request,
            api_key=ctx.api_key,
        )
    except (PermissionDenied, ValidationError) as exc:
        raise _service_error(exc) from exc
    return _comment_brief(comment)


# ---------------------------------------------------------------------------
# Outbound events (webhooks and saved searches)
# ---------------------------------------------------------------------------
def _webhook_brief(webhook) -> dict:
    return {
        "id": str(webhook.pk),
        "name": webhook.name,
        "events": webhook.events,
        "enabled": webhook.enabled,
        "target": str(webhook.target_id),
        "workspace": str(webhook.workspace_id) if webhook.workspace_id else None,
    }


@tool(
    "webhook_list",
    "List your outbound webhook subscriptions.",
    {"type": "object", "properties": {}},
)
def tool_webhook_list(ctx: ToolContext, args: dict) -> dict:
    from apps.events.models import Webhook

    webhooks = Webhook.objects.filter(owner=ctx.user).select_related("target")
    return {"webhooks": [_webhook_brief(webhook) for webhook in webhooks]}


@tool(
    "webhook_create",
    "Subscribe to events; each is delivered as a signed POST through a gateway target.",
    {
        "type": "object",
        "properties": {
            "name": {"type": "string"},
            "target": {"type": "string", "description": "Gateway target id or name"},
            "events": {"type": "array", "items": {"type": "string"}},
            "signing_secret_id": {"type": "string", "description": "Optional HMAC secret"},
            "workspace": {"type": "string", "description": "Optional workspace scope"},
        },
        "required": ["name", "target", "events"],
    },
)
def tool_webhook_create(ctx: ToolContext, args: dict) -> dict:
    from apps.events.models import Webhook

    target = GatewayService.resolve(args["target"])
    _require(ctx, target.resource, Permission.USE)
    workspace = _get_workspace(args["workspace"]) if args.get("workspace") else None
    if workspace is not None:
        _require(ctx, workspace.resource, Permission.WRITE)
    secret = _owned_secret(ctx, args.get("signing_secret_id"))
    webhook = Webhook.objects.create(
        name=args["name"],
        owner=ctx.user,
        workspace=workspace,
        target=target,
        signing_secret=secret,
        events=list(args.get("events") or []),
    )
    return _webhook_brief(webhook)


@tool(
    "saved_search_list",
    "List your saved searches (the system re-runs them and notifies on new matches).",
    {"type": "object", "properties": {}},
)
def tool_saved_search_list(ctx: ToolContext, args: dict) -> dict:
    from apps.events.models import SavedSearch

    rows = SavedSearch.objects.filter(owner=ctx.user)
    return {
        "saved_searches": [
            {
                "id": str(row.pk),
                "name": row.name,
                "query": row.query,
                "enabled": row.enabled,
                "webhook": str(row.webhook_id) if row.webhook_id else None,
            }
            for row in rows
        ]
    }


@tool(
    "saved_search_create",
    "Save a query the system re-runs, notifying on new matches.",
    {
        "type": "object",
        "properties": {
            "name": {"type": "string"},
            "query": {"type": "string"},
            "mode": {"type": "string", "enum": ["text", "semantic", "hybrid"]},
            "workspace": {"type": "string"},
            "webhook": {"type": "string", "description": "One of your webhooks to notify"},
        },
        "required": ["name", "query"],
    },
)
def tool_saved_search_create(ctx: ToolContext, args: dict) -> dict:
    from apps.events.models import SavedSearch, Webhook

    workspace = _get_workspace(args["workspace"]) if args.get("workspace") else None
    webhook = None
    if args.get("webhook"):
        webhook = Webhook.objects.filter(pk=args["webhook"], owner=ctx.user).first()
        if webhook is None:
            raise ToolError("A webhook nem a tiéd vagy nem létezik.")
    saved = SavedSearch.objects.create(
        name=args["name"],
        owner=ctx.user,
        workspace=workspace,
        query=args["query"],
        mode=args.get("mode", "hybrid"),
        webhook=webhook,
    )
    return {"id": str(saved.pk), "name": saved.name, "query": saved.query}


# ---------------------------------------------------------------------------
# Consolidated memory
# ---------------------------------------------------------------------------
@tool(
    "memory_view",
    "The consolidated view: facts you may read, grouped by subject, each cited "
    "back to the document it came from. Only your own ACL boundary.",
    {
        "type": "object",
        "properties": {
            "subject": {"type": "string", "description": "Only facts about this subject"},
            "workspace": {"type": "string"},
        },
    },
)
def tool_memory_view(ctx: ToolContext, args: dict) -> dict:
    from apps.memory.services import FactService

    workspace_id = None
    if args.get("workspace"):
        workspace_id = _get_workspace(args["workspace"]).pk
    return FactService.view(
        ctx.user,
        subject=args.get("subject"),
        workspace=workspace_id,
        api_key=ctx.api_key,
    )


@tool(
    "memory_extract",
    "Rebuild consolidated-memory facts from documents (one workspace, or all).",
    {
        "type": "object",
        "properties": {"workspace": {"type": "string"}},
    },
)
def tool_memory_extract(ctx: ToolContext, args: dict) -> dict:
    from apps.memory.services import FactService

    if args.get("workspace"):
        from apps.documents.models import Document

        workspace = _get_workspace(args["workspace"])
        _require(ctx, workspace.resource, Permission.WRITE)
        count = 0
        for document in Document.objects.filter(workspace=workspace).select_related(
            "workspace", "project", "resource"
        ):
            count += FactService.extract_document(document)
        return {"facts": count}
    if not getattr(ctx.user, "is_superuser", False):
        raise ToolError("A teljes újrakinyerés superuser.")
    return FactService.rebuild_all()


@tool(
    "memory_reflect",
    "Recreate the reflection step natively: summarise what is known about a subject "
    "from the facts you may read, and store it as an evidence-backed aggregate. Uses "
    "the platform LLM provider (offline it falls back to the deterministic roll-up).",
    {
        "type": "object",
        "properties": {
            "subject": {"type": "string"},
            "workspace": {"type": "string"},
        },
        "required": ["subject"],
    },
)
def tool_memory_reflect(ctx: ToolContext, args: dict) -> dict:
    from apps.memory.services import ReflectionService

    workspace_id = None
    if args.get("workspace"):
        workspace_id = _get_workspace(args["workspace"]).pk
    text = ReflectionService.reflect(
        args["subject"], user=ctx.user, workspace=workspace_id, api_key=ctx.api_key
    )
    if text is None:
        raise ToolError("Nincs olvasható tény ehhez a subjecthez.")
    return {"subject": args["subject"], "aggregate": text}
