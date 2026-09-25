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

from apps.documents.models import ChangeSource, Document
from apps.documents.services import DocumentService
from apps.git.git_cli import GitError
from apps.git.models import GitRepository
from apps.git.services import GitService
from apps.permissions.constants import Permission
from apps.permissions.services import PermissionService
from apps.resources.models import Resource
from apps.search.services import SearchService
from apps.secrets.models import Secret
from apps.secrets.services import SecretService
from apps.workspaces.models import Project, Workspace
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
    except (Document.DoesNotExist, ValueError, TypeError) as exc:
        raise ToolError(f"Document '{document_id}' not found.") from exc


def _get_workspace(value) -> Workspace:
    queryset = Workspace.objects.select_related("resource")
    try:
        return queryset.get(pk=value) if str(value).count("-") >= 4 else queryset.get(slug=value)
    except (Workspace.DoesNotExist, ValueError):
        raise ToolError(f"Workspace '{value}' not found.") from None


def _get_project(value) -> Project:
    try:
        return Project.objects.select_related("resource", "workspace").get(pk=value)
    except (Project.DoesNotExist, ValueError):
        raise ToolError(f"Project '{value}' not found.") from None


def _get_repository(value) -> GitRepository:
    try:
        return GitRepository.objects.select_related("workspace", "project", "resource").get(pk=value)
    except (GitRepository.DoesNotExist, ValueError):
        raise ToolError(f"Git repository '{value}' not found.") from None


def _document_brief(document: Document) -> dict:
    return {
        "id": str(document.pk),
        "title": document.title,
        "path": document.path,
        "status": document.status,
        "priority": document.priority,
        "summary": document.summary,
        "workspace": str(document.workspace_id),
        "project": str(document.project_id) if document.project_id else None,
    }


def _accessible_documents(ctx, *, workspace=None, project=None) -> list[Document]:
    queryset = Document.objects.select_related("workspace", "project", "resource")
    if workspace is not None:
        queryset = queryset.filter(workspace=workspace)
    if project is not None:
        queryset = queryset.filter(project=project)
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
    "Fetch only the title/summary. May be available when content is restricted.",
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
    elif (document.resource.metadata or {}).get("public_summary"):
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


@tool("knowledge_list_workspaces", "List workspaces the caller can read.")
def tool_list_workspaces(ctx: ToolContext, args: dict) -> dict:
    workspaces = [
        {"id": str(workspace.pk), "name": workspace.name, "slug": workspace.slug}
        for workspace in Workspace.objects.select_related("resource")
        if _may_read(ctx, workspace.resource)
    ]
    return {"workspaces": workspaces}


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
    "List documents (optionally scoped to a workspace/project).",
    {
        "type": "object",
        "properties": {"workspace": {"type": "string"}, "project": {"type": "string"}},
    },
)
def tool_list_documents(ctx: ToolContext, args: dict) -> dict:
    workspace = _get_workspace(args["workspace"]) if args.get("workspace") else None
    project = _get_project(args["project"]) if args.get("project") else None
    documents = _accessible_documents(ctx, workspace=workspace, project=project)
    return {"documents": [_document_brief(document) for document in documents]}


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
    "related knowledge across projects/workspaces.",
    {
        "type": "object",
        "properties": {
            "resource_id": {"type": "string"},
            "direction": {"type": "string", "enum": ["outgoing", "incoming"]},
        },
        "required": ["resource_id"],
    },
)
def tool_follow_link(ctx: ToolContext, args: dict) -> dict:
    try:
        resource = Resource.objects.get(pk=args.get("resource_id"))
    except (Resource.DoesNotExist, ValueError, TypeError) as exc:
        raise ToolError("Resource not found.") from exc
    _require(ctx, resource, Permission.READ)

    direction = args.get("direction", "outgoing")
    links = (
        resource.incoming_links.select_related("source")
        if direction == "incoming"
        else resource.outgoing_links.select_related("target")
    )
    out = []
    for link in links:
        target = link.source if direction == "incoming" else link.target
        entry = {
            "resource_id": str(target.id),
            "name": target.name,
            "type": target.resource_type,
            "link_type": link.link_type,
            "accessible": _may_read(ctx, target),
            "summary_visible": bool((target.metadata or {}).get("public_summary")),
        }
        out.append(entry)
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
    target = project.resource if project else workspace.resource
    _require(ctx, target, Permission.WRITE)
    document = DocumentService.create(
        workspace=workspace,
        project=project,
        title=args.get("title", ""),
        path=args.get("path"),
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
