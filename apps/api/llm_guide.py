"""Self-describing agent manifest.

``GET /llm`` answers a single question: how does a model configure itself to act
as the AI of this Brainbox instance? The payload is generated from the live
registries -- the DRF router, the MCP tool registry and the runtime setting
definitions -- so the manifest can never drift away from the code it describes.

Never leaks credentials: settings are listed with their definition metadata and,
for superusers only, with the already-masked current value from
``settings_store.services.describe()``.
"""

from __future__ import annotations

import json
import re

from django.views.decorators.http import require_GET
from rest_framework.exceptions import AuthenticationFailed

from apps.accounts.authentication import resolve_api_key

# HTTP verb -> permission the engine requires for that verb. Mirrors
# ``apps.api.permissions.METHOD_PERMISSION`` so the manifest never drifts.
from .permissions import METHOD_PERMISSION

API_BASE = "/api/v1"
MCP_PATH = "/mcp"

_METHOD_PERMISSION = {method: value for method, value in METHOD_PERMISSION.items()}

# A DRF ``@action(url_path=r"versions/(?P<n>[0-9]+)")`` arrives as a regex; an
# agent reads a URL, not a pattern, so the named groups become ``{n}``.
_ROUTE_PARAM = re.compile(r"\(\?P<(\w+)>[^)]+\)")

_PERMISSION_LABELS = {
    "IsAuthenticated": "any authenticated caller",
    "IsAdminUser": "superuser only",
    "AllowAny": "public (no auth)",
    "ResourcePermission": "caller must hold the permission on the target resource (ACL aware)",
}

_ROUTER_ACTIONS = {
    "list": ("collection", ["GET"]),
    "create": ("collection", ["POST"]),
    "retrieve": ("detail", ["GET"]),
    "update": ("detail", ["PUT"]),
    "partial_update": ("detail", ["PATCH"]),
    "destroy": ("detail", ["DELETE"]),
}


# ---------------------------------------------------------------------------
# Introspection helpers
# ---------------------------------------------------------------------------
def _access_rules(target) -> list[str]:
    classes = getattr(target, "permission_classes", []) or []
    rules = []
    for entry in classes:
        name = getattr(entry, "__name__", str(entry))
        rules.append(_PERMISSION_LABELS.get(name, name))
    return rules or ["default: any authenticated caller"]


def _lookup_placeholder(viewset) -> str:
    field = getattr(viewset, "lookup_field", "pk") or "pk"
    return field if field != "pk" else "id"


def _rest_endpoints() -> list[dict]:
    """Every router-registered route, derived from the DRF router itself."""
    from apps.api.urls import router

    endpoints: list[dict] = []
    for prefix, viewset, _basename in router.registry:
        lookup = _lookup_placeholder(viewset)
        access = _access_rules(viewset)
        collection = f"{API_BASE}/{prefix}/"
        detail = f"{API_BASE}/{prefix}/{{{lookup}}}/"
        seen: dict[str, list[str]] = {}

        for action_name, (target, methods) in _ROUTER_ACTIONS.items():
            if not hasattr(viewset, action_name):
                continue
            path = collection if target == "collection" else detail
            seen.setdefault(path, []).extend(methods)

        for attr in dir(viewset):
            member = getattr(viewset, attr, None)
            mapping = getattr(member, "mapping", None)
            if mapping is None:
                continue
            url_path = getattr(member, "url_path", None) or attr.replace("_", "-")
            url_path = _ROUTE_PARAM.sub(r"{\1}", url_path)
            path = f"{detail}{url_path}/" if member.detail else f"{collection}{url_path}/"
            seen.setdefault(path, []).extend(sorted(mapping))

        for path, methods in seen.items():
            verbs = sorted({verb.upper() for verb in methods if verb.upper() in _METHOD_PERMISSION})
            endpoints.append(
                {
                    "path": path,
                    "methods": verbs,
                    "requires": sorted(
                        {_METHOD_PERMISSION[verb] for verb in verbs if verb in _METHOD_PERMISSION}
                    ),
                    "access": access,
                }
            )
    return sorted(endpoints, key=lambda row: row["path"])


def _function_endpoints() -> list[dict]:
    """The non-router APIView paths (llm, search, discovery, quality, drafts)."""
    from apps.api.urls import router, urlpatterns

    # `urlpatterns` is built as `router.urls + [...]`, so slicing past the router
    # is exact. String matching on the pattern would not be: a Django RoutePattern
    # renders as its regex (``^documents/(?P<pk>[^/.]+)/$``), and every router
    # route would leak in again as a duplicate with no methods.
    endpoints = []
    for pattern in list(urlpatterns)[len(router.urls) :]:
        view = getattr(pattern.callback, "cls", None)
        if view is None:
            continue
        route = getattr(pattern.pattern, "_route", str(pattern.pattern))
        verbs = sorted(
            verb.upper()
            for verb in ("get", "post", "put", "patch", "delete")
            if hasattr(view, verb)
        )
        endpoints.append(
            {
                "path": f"{API_BASE}/{route}",
                "methods": verbs,
                "requires": sorted(
                    {_METHOD_PERMISSION[verb] for verb in verbs if verb in _METHOD_PERMISSION}
                ),
                "access": _access_rules(view),
            }
        )
    return sorted(endpoints, key=lambda row: row["path"])


def _mcp_section() -> dict:
    from apps.mcp import tools
    from apps.mcp.server import PROTOCOL_VERSION, SERVER_NAME, SERVER_VERSION

    return {
        "endpoint": MCP_PATH,
        "protocol": "MCP over JSON-RPC 2.0, HTTP POST",
        "protocol_version": PROTOCOL_VERSION,
        "server": {"name": SERVER_NAME, "version": SERVER_VERSION},
        "transport_notes": [
            "One HTTP POST per JSON-RPC request; the response is application/json.",
            "No SSE stream and no session id: stateless, safe to retry.",
            "Batching is supported: POST a JSON array of requests.",
            "GET /mcp returns server info without authentication.",
        ],
        "methods": {
            "initialize": "Handshake. Returns protocolVersion, capabilities, serverInfo.",
            "notifications/initialized": "Acknowledge the handshake (no response body).",
            "ping": "Liveness check, returns an empty result.",
            "tools/list": "Return the tool catalogue (same payload as this section).",
            "tools/call": "params: {name, arguments}. Execute one tool.",
        },
        "result_convention": {
            "success": "result.content[0].text holds JSON (or plain text), isError=false.",
            "tool_error": "Permission/validation problems come back as isError=true with a "
            "human-readable message (e.g. \"Permission denied.\"), not as a JSON-RPC error.",
            "protocol_error": "JSON-RPC error object: -32600 invalid request, -32601 unknown "
            "method, -32602 bad params, -32700 parse error, -32001 authentication.",
        },
        "tool_count": len(tools.definitions()),
        "tools": tools.definitions(),
    }


def _settings_section(user) -> dict:
    from apps.settings_store.services import DEFINITIONS, describe

    definition_rows = [
        {
            "key": definition.key,
            "label": definition.label,
            "category": definition.category,
            "value_type": definition.value_type,
            "choices": list(definition.choices),
            "secret": definition.secret,
            "requires_restart": definition.requires_restart,
            "description": definition.description,
            "example": definition.example,
        }
        for definition in DEFINITIONS
    ]

    is_superuser = bool(user and user.is_authenticated and user.is_superuser)
    rows = definition_rows
    if is_superuser:
        # describe() masks secret values ("***"); no credential ever leaves here.
        rows = [
            {**definition, "current": live.get("current"), "overridden": live["overridden"]}
            for definition, live in zip(definition_rows, describe(), strict=True)
        ]

    return {
        "catalog": rows,
        "values_visible": is_superuser,
        "read": f"GET {API_BASE}/settings/ (superuser) or MCP knowledge_get_settings",
        "write": f"PATCH {API_BASE}/settings/<KEY>/ {{\"value\": \"...\"}} (superuser) or MCP "
        "knowledge_set_setting; PATCH {\"reset\": true} drops the DB override and falls back to "
        "the environment value",
        "note": "Stored overrides live in the DB and win over the environment; secret values are "
        "returned masked and can only be read back through the secret vault.",
    }


def _caller_section(user, api_key) -> dict | None:
    """Who is asking. Never echoes a key value."""
    if user is None or not getattr(user, "is_authenticated", False):
        return None

    caller = {
        "username": user.get_username(),
        "is_superuser": bool(user.is_superuser),
        "user_id": str(user.pk),
    }
    if api_key is not None:
        caller["api_key"] = {
            "name": api_key.name,
            "prefix": api_key.key_prefix,
            "expires_at": api_key.expires_at.isoformat() if api_key.expires_at else None,
            "usable": api_key.is_usable,
            "scopes": [
                {
                    "workspace": str(scope.workspace_id) if scope.workspace_id else None,
                    "project": str(scope.project_id) if scope.project_id else None,
                    "permission": scope.permission,
                    "effect": scope.effect,
                }
                for scope in api_key.scopes.all()
            ],
        }
    return caller


# ---------------------------------------------------------------------------
# Static guidance
# ---------------------------------------------------------------------------
def _authentication_section() -> dict:
    return {
        "schemes": ["session (browser/admin only)", "api key"],
        "header": "Authorization: ApiKey <API_KEY>",
        "alternative_header": "X-API-Key: <API_KEY>",
        "not_supported": [
            "Authorization: Bearer <token> is NOT accepted on /api/v1 or /mcp.",
            "Bearer is only used by /metrics (BRAINBOX_METRICS_TOKEN).",
        ],
        "how_to_get_a_key": {
            "ui": "GET /manage/api-keys/ (signed in as a human)",
            "api": f"POST {API_BASE}/api-keys/ with a session, body "
            '{\"name\": \"my-agent\", \"scopes\": [], \"expires_at\": null} -- requires a session, '
            "an API key cannot mint another API key",
            "raw_key_returned": "Exactly once, in the create response (field \"key\"). Only a "
            "hash is stored, so it cannot be re-read or recovered.",
        },
        "key_properties": {
            "format": "prefix + secret; the first 16 characters are the lookup prefix",
            "inherits": "the owning user's permissions -- a key can never exceed its user",
            "scopes": "optional narrowing to workspace/project with an allow/deny permission; "
            "a global scope (no workspace and no project) means unrestricted",
            "lifecycle": "active / revoked / expired; last_used_at is refreshed on every call",
        },
        "failures": {
            "401": "Missing, malformed, revoked or expired key.",
            "403": "Authenticated, but the ACL denies the required permission on the resource.",
            "note": "401 on the manifest itself only happens with a bad key; an anonymous read "
            "returns the manifest with current_caller=null.",
        },
    }


def _conventions() -> list[dict]:
    return [
        {
            "rule": "Everything you write is a draft until a human approves it.",
            "why": "AI-created documents are stored with status=draft; search ranks approved "
            "knowledge above drafts, and the draft is invisible to consumers who only read "
            "approved content.",
            "how": "Create with knowledge_create_document / POST /api/v1/documents/ (or "
            "knowledge_generate_draft for an LLM-written draft). Approval is a human action: "
            f"POST {API_BASE}/documents/<id>/approve/ or POST {API_BASE}/documents/<id>/reject/ "
            "(MCP: knowledge_approve_document).",
        },
        {
            "rule": "Search before you write.",
            "why": "Duplicates are the main failure mode of AI knowledge capture; the base is a "
            "file tree, so a second file with the same meaning shadows nothing, it just drifts.",
            "how": "knowledge_search (hybrid mode) or GET /api/v1/search/?q=... -- update the "
            "existing document instead of creating a new one.",
        },
        {
            "rule": "Never put a secret value into a document, a summary or a log line.",
            "why": "Documents are plain markdown on disk and fully searchable; the secret vault "
            "exists exactly to keep values out of that plane. A redacting scanner is a safety "
            "net, not a licence.",
            "how": "Reference the secret by name/id and resolve it at use time with "
            "knowledge_get_metadata + secret_use (audited).",
        },
        {
            "rule": "Resolve ids, never invent them.",
            "why": "Every object id is a UUID and a wrong id is a 404, not a silent no-op.",
            "how": "knowledge_list_workspaces / knowledge_list_projects / knowledge_list_documents "
            f"first. In MCP, a workspace argument also accepts its slug "
            f"(GET {API_BASE}/workspaces/<slug>/ works the same way).",
        },
        {
            "rule": "Address a node by its tree path when you have one.",
            "why": "A project and a folder are the same kind of tree node, and the "
            "workspace-relative tree path names either one unambiguously, where a "
            "project id plus a scope-relative path has to be assembled by hand.",
            "how": "Every document/file/folder response carries tree_path. Place a "
            "document with knowledge_create_document using node=\"Deploy/dotnet/x.md\", "
            f"or a folder via POST {API_BASE}/folders/ with node=\"Deploy/runbooks\"; "
            "move with node as the destination. project + path still work.",
        },
        {
            "rule": "Reach an external service through a gateway target, not a stored credential.",
            "why": "Tokens for GitHub, other MCP servers and internal APIs live in the "
            "vault behind a target; your key never holds them, and every call is "
            "permission-checked (USE) and audited.",
            "how": "gateway_list, then gateway_call(target, method, path, body) or "
            f"gateway_mcp to call another MCP server's tool. REST: "
            f"POST {API_BASE}/gateway/<id>/call/. Configure a target with "
            "knowledge_create_gateway_target / _update_ / _delete_ (needs write on "
            "the workspace; the credential must be one of your own secrets).",
        },
        {
            "rule": "Tidy-ups are proposals, not edits: decide them through the curator.",
            "why": "The system scans for drift (exact duplicates, passed review dates) "
            "and writes a proposal instead of changing knowledge silently. Approving "
            "applies it through the normal services (ACL, audit, versioning); a "
            "rejection is remembered, so the scan stops suggesting it.",
            "how": "curator_list_proposals, then curator_approve / curator_reject; "
            f"curator_scan runs a scan on demand. REST: {API_BASE}/curator/.",
        },
        {
            "rule": "Updates create a new version; deletion is the last resort.",
            "why": "Each write appends an immutable DocumentVersion with the author, source and "
            "git commit, so history stays auditable. Knowledge that is obsolete is marked, not "
            "removed.",
            "how": "knowledge_update_document / PUT-PATCH /api/v1/documents/<id>/. Set status to "
            "deprecated or archived instead of deleting. Versions: "
            f"GET {API_BASE}/documents/<id>/versions/ (+ /restore/).",
        },
        {
            "rule": "Read what you can reach, write only what you may write.",
            "why": "The permission engine is evaluated per resource: read < write < delete < "
            "admin, admin implies everything, an explicit deny always beats an allow, and "
            "inheritance runs workspace -> project -> document.",
            "how": "List endpoints are already filtered to what you may read. A 403 means the "
            "ACL denied it; do not retry, ask for the grant.",
        },
        {
            "rule": "Everything is audited, so keep writes deliberate and small.",
            "why": "Every read and write produces an AuditEvent tagged with the source "
            "(api | mcp | web | git | import | system), the user, the API key and the IP.",
            "how": f"Review with GET {API_BASE}/audit/ or /manage/audit/.",
        },
        {
            "rule": "Configuration is runtime-mutable, but only by a superuser.",
            "why": "AI/embedding/search/git/job settings live in the database as overrides on top "
            "of the environment, so they can change without a redeploy -- and a wrong value "
            "breaks search for everyone.",
            "how": "knowledge_get_settings to inspect, knowledge_set_setting to change, then "
            "re-verify with GET /readyz. Changes are audited.",
        },
        {
            "rule": "Report tool errors to the user verbatim.",
            "why": "MCP wraps failures as isError=true with a short message instead of a "
            "protocol error, so the text is the only diagnostic you get.",
            "how": '"Permission denied." = ACL, "not found" = wrong id, "Unknown tool" = '
            "protocol drift (re-run tools/list).",
        },
    ]


def _workflows() -> list[dict]:
    return [
        {
            "name": "first_contact",
            "goal": "Find out what this instance knows and what you may see.",
            "steps": [
                "GET /llm (this document)",
                f"GET {API_BASE}/workspaces/ and {API_BASE}/projects/",
                "MCP: knowledge_list_projects, then knowledge_search({'query': '<topic>'})",
                "GET /readyz -- tells you whether search and embeddings are actually usable",
            ],
        },
        {
            "name": "answer_with_citations",
            "goal": "Ground an answer in company knowledge instead of guessing.",
            "steps": [
                "knowledge_search (mode=hybrid, workspace/project filter if known)",
                "knowledge_get on the top hits to read the full text",
                "knowledge_follow_link / knowledge_get_related to widen the context",
                "Cite document ids and paths in the answer; say so when nothing matches.",
            ],
        },
        {
            "name": "capture_knowledge",
            "goal": "Store something new without polluting the base.",
            "steps": [
                "knowledge_search first -- update a match instead of duplicating it",
                "knowledge_create_document (status=draft, path under the workspace/project)",
                "or POST /api/v1/drafts/ to have the configured LLM write the draft for you",
                "Stop. Let a human approve it (approve/reject). Approval needs write access "
                "and nothing stops you from using it on your own draft, so this is a rule "
                "about how you behave, not one the API enforces: never approve your own "
                "work.",
            ],
        },
        {
            "name": "maintain_knowledge",
            "goal": "Keep existing documents correct.",
            "steps": [
                "knowledge_get to read the current content",
                "knowledge_update_document -- a new version is appended automatically",
                "Move status along: draft -> approved (human), approved -> deprecated when "
                "superseded",
            ],
        },
        {
            "name": "review_queue",
            "goal": "Work through what is waiting for a decision.",
            "steps": [
                "GET /api/v1/documents/?status=draft (or knowledge_list_documents with "
                "type_name/status)",
                "GET /api/v1/deadlines/ or knowledge_deadlines for dated items",
                f"POST {API_BASE}/documents/<id>/approve/ or /reject/ (human decision)",
            ],
        },
        {
            "name": "configure_the_platform",
            "goal": "Tune AI, search and git behaviour at runtime (superuser).",
            "steps": [
                "knowledge_get_settings -- read the masked catalog",
                "knowledge_set_setting for a single key, or PATCH /api/v1/settings/<KEY>/",
                "GET /readyz to confirm the change took effect",
                "Keys marked requires_restart need a container restart.",
            ],
        },
        {
            "name": "use_a_credential",
            "goal": "Act with a secret without ever seeing it stored in knowledge.",
            "steps": [
                "knowledge_get_metadata(secret_id) -- inspect without the value",
                "secret_use(secret_id) -- returns the value for injection, audited per use",
                "Use it in the call, never copy it into a document.",
            ],
        },
        {
            "name": "ship_changes_as_git",
            "goal": "Route knowledge changes through review.",
            "steps": [
                "knowledge_get_git_status to see the checkout state",
                "knowledge_create_branch, then knowledge_create_commit",
                "knowledge_create_pull_request (needs BRAINBOX_GITHUB_TOKEN)",
            ],
        },
    ]


def _domain_model() -> dict:
    return {
        "tree": "workspace -> project -> (documents | files | folders | git repository | secrets). "
        "Every object also has a Resource row (UUID) that is the unit of the ACL.",
        "identity": "For workspace, project and document the model id IS the resource id, so the "
        "same uuid works in an ACL row and in an API path.",
        "entities": {
            "workspace": "Top-level container. id = slug or uuid in MCP, uuid in REST.",
            "project": "Optional second level inside a workspace.",
            "document": "Markdown knowledge file. Has title, slug, path (relative, unique per "
            "folder), summary, status, priority, frontmatter, metadata, tags and links. The "
            "file on disk is the source of truth; the DB row is metadata.",
            "document_version": "Immutable snapshot per write: version number, content, summary, "
            "change_type, change_source, author, api_key, git_commit.",
            "document_folder": "Virtual folder path per workspace/project, used to group documents.",
            "file": "Binary or non-markdown attachment with checksum and versions.",
            "link": "ResourceLink between two resources: reference | relates_to | depends_on | "
            "supersedes (see /api/v1/links/).",
            "tag": "Free-form label on documents and folders.",
            "secret": "Encrypted credential. Only metadata is listable; the value needs an "
            "explicit, audited use.",
            "deadline": "Dated item extracted from knowledge, drives /calendar/ and the agenda.",
            "git_repository": "Repository attached to a workspace/project; checkout is the "
            "source of truth, platform writes commit back.",
            "api_key": "Per-user credential with scopes; drives both /api/v1 and /mcp.",
            "group": "Named user set, usable as an ACL subject.",
            "resource_acl": "The ACL row: subject (user | group | api_key) x permission x effect "
            "(allow | deny), scoped to a resource.",
            "job / job_run": "Scheduled background work (reindex, sync, prune) and its runs.",
            "runtime_setting": "DB override on top of an environment setting.",
        },
        "vocabularies": {
            "permission": ["read", "use", "write", "delete", "admin"],
            "effect": ["allow", "deny"],
            "document_status": ["draft", "experimental", "approved", "deprecated", "archived"],
            "change_source": ["web", "api", "mcp", "git", "import", "system"],
            "change_type": ["create", "update", "restore", "import", "ai", "delete"],
            "search_mode": ["text", "semantic", "hybrid"],
        },
    }


def _client_configs(base_url: str) -> list[dict]:
    return [
        {
            "target": "Any MCP client that speaks JSON-RPC 2.0 over HTTP",
            "config": {
                "url": f"{base_url}{MCP_PATH}",
                "headers": {"Authorization": "ApiKey <API_KEY>"},
            },
            "handshake": {
                "method": "initialize",
                "params": {
                    "protocolVersion": "2024-11-05",
                    "capabilities": {},
                    "clientInfo": {"name": "<your-agent>", "version": "1.0.0"},
                },
            },
        },
        {
            "target": "opencode-style remote MCP entry",
            "config": {
                "mcp": {
                    "brainbox": {
                        "type": "remote",
                        "url": f"{base_url}{MCP_PATH}",
                        "enabled": True,
                        "headers": {"Authorization": "ApiKey <API_KEY>"},
                    }
                }
            },
        },
        {
            "target": "stdio-only clients via the mcp-remote bridge",
            "config": {
                "command": "npx",
                "args": ["-y", "mcp-remote", f"{base_url}{MCP_PATH}", "--header",
                         "Authorization: ApiKey <API_KEY>"],
            },
        },
    ]


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
def build_manifest(request=None, *, user=None, api_key=None) -> dict:
    """Assemble the full manifest.

    ``request`` may be a Django or a DRF request. ``user``/``api_key`` can be
    passed explicitly (the HTML page resolves the key itself, since a plain
    Django request has no ``auth`` attribute for the DRF helper to read).
    """
    if request is None and user is None:
        raise ValueError("build_manifest needs a request or an explicit user")
    if user is None:
        user = getattr(request, "user", None)
    if api_key is None and request is not None:
        from .permissions import api_key_from_request

        api_key = api_key_from_request(request)

    base_url = ""
    if request is not None:
        base_url = request.build_absolute_uri("/").rstrip("/")

    caller = _caller_section(user, api_key)

    return {
        "manifest_version": "1.0",
        "what_this_is": "Everything a model needs to configure itself as the AI of this "
        "Brainbox instance: how to authenticate with an API key, which endpoints and tools "
        "exist, the domain model, the rules an agent must follow, and the runtime settings.",
        "service": {
            "name": "brainbox",
            "role": "company knowledge base with an agent-facing API and MCP server",
            "generated_for": "LLM agents",
            "base_url": base_url,
            "docs": {
                "manifest": f"{base_url}/llm",
                "manifest_json": f"{base_url}{API_BASE}/llm/",
                "human_readme": f"{base_url}/manage/settings/",
            },
            "health": {
                "liveness": f"{base_url}/healthz",
                "readiness": f"{base_url}/readyz",
                "metrics": f"{base_url}/metrics (Bearer BRAINBOX_METRICS_TOKEN when set)",
            },
        },
        "configure_yourself": [
            {
                "step": 1,
                "action": "Read this manifest.",
                "request": f"GET {base_url}/llm",
                "auth": "none",
            },
            {
                "step": 2,
                "action": "Get an API key from a human (UI or API with a session). The raw key "
                "is shown exactly once.",
                "request": f"GET {base_url}/manage/api-keys/ or POST {base_url}{API_BASE}/api-keys/",
                "auth": "session",
            },
            {
                "step": 3,
                "action": "Send it as 'Authorization: ApiKey <key>' on every call to /api/v1 and "
                "/mcp. Nothing else needs configuring -- no SDK, no session, no CSRF token.",
                "request": f"GET {base_url}{API_BASE}/workspaces/",
                "auth": "api key",
            },
            {
                "step": 4,
                "action": "Scope discovery: list what you can see before you assume anything.",
                "request": f"GET {base_url}{API_BASE}/workspaces/ , GET {base_url}{API_BASE}/projects/",
                "auth": "api key",
            },
            {
                "step": 5,
                "action": "Optionally narrow the key with scopes so the agent cannot reach more "
                "than its job requires (workspace/project + allow/deny permission).",
                "request": f"POST {base_url}{API_BASE}/api-keys/ with scopes[]",
                "auth": "session",
            },
            {
                "step": 6,
                "action": "Prefer MCP over REST for agent work: one connection, self-describing "
                "tools, permission checks already applied. REST is the escape hatch for "
                "pagination, bulk upload and fine-grained control.",
                "request": f"POST {base_url}{MCP_PATH}",
                "auth": "api key",
            },
            {
                "step": 7,
                "action": "Read conventions and workflows below, then start with "
                "knowledge_search.",
                "request": "see this document",
                "auth": "none",
            },
        ],
        "current_caller": caller,
        "authentication": _authentication_section(),
        "transports": {
            "mcp": _mcp_section(),
            "rest": {
                "base": f"{base_url}{API_BASE}/",
                "root": f"{base_url}/llm",
                "trailing_slash": "APPEND_SLASH is on; always keep the trailing slash or you "
                "get a 301.",
                "pagination": "PageNumberPagination, default page size 50: use ?page=N and "
                "?page_size=N, results are in {count, next, previous, results}.",
                "filtering": "SearchFilter (?search=) and OrderingFilter (?ordering=) are enabled "
                "where the viewset declares the fields.",
                "conventions": {
                    "auth": "Authorization: ApiKey <key> (or X-API-Key). Sessions work in a "
                    "browser; Bearer does not.",
                    "ids": "All ids are UUIDs. Related objects are referenced by id in the "
                    "request body.",
                    "writes": "POST create, PUT full replace, PATCH partial, DELETE. Every write "
                    "needs write (or admin) permission on the target resource.",
                    "custom_actions": "Sub-paths on a detail route (approve, versions, restore, "
                    "move, revoke, ...) are listed in endpoints below.",
                },
                "endpoints": _rest_endpoints() + _function_endpoints(),
            },
            "client_configs": _client_configs(base_url),
        },
        "domain_model": _domain_model(),
        "conventions": _conventions(),
        "workflows": _workflows(),
        "runtime_settings": _settings_section(user),
        "errors": {
            "400": "Validation error; the body maps field -> message.",
            "401": "Missing/invalid/revoked/expired API key.",
            "403": "Authenticated but the ACL denies the required permission.",
            "404": "Unknown id, or an object you may not read (existence is not leaked).",
            "405": "Wrong verb for that path.",
            "mcp_is_error": "Tool-level problems are returned as content with isError=true; the "
            "text is the diagnostic.",
        },
        "guardrails": [
            "Never reveal an API key, secret value or session cookie in output.",
            "Do not call /metrics, /admin/ or superuser-only endpoints with an agent key.",
            "Prefer draft + human approval over direct approved writes.",
            "When the ACL denies something, report it; do not look for a way around it.",
            "Stay inside the configured workspace/project unless the user names another.",
        ],
    }


# ---------------------------------------------------------------------------
# HTML surface
# ---------------------------------------------------------------------------
def _pretty(value) -> str:
    return json.dumps(value, indent=2, ensure_ascii=False, default=str)


def _curl(base_url: str, path: str, *, api_key: bool = True) -> str:
    header = ' -H "X-API-Key: <API_KEY>"' if api_key else ""
    return f"curl{header} \"{base_url}{path}\""


def _wants_html(request) -> bool:
    """Browsers get the page, agents get JSON.

    A browser always sends ``text/html``; curl and API clients send ``*/*`` or
    ``application/json``, and those must keep receiving machine-readable output.
    """
    forced = request.GET.get("format")
    if forced in {"html", "json"}:
        return forced == "html"
    if forced:
        return False
    return "text/html" in request.META.get("HTTP_ACCEPT", "")


@require_GET
def llm_page(request):
    """``GET /llm`` -- the manifest as a readable page (browsers) or JSON.

    A plain Django view rather than a DRF APIView: the page needs Django
    templates, and DRF cannot negotiate ``text/html`` without a browsable
    renderer (which would also expose every REST collection as a browsable
    form). The JSON contract lives on ``/api/v1/llm/``.
    """
    from django.http import HttpResponse, JsonResponse
    from django.shortcuts import render

    try:
        key_user, api_key = resolve_api_key(request)
    except AuthenticationFailed as exc:
        return HttpResponse(f"401 - {exc.detail}\n", status=401, content_type="text/plain")

    manifest = build_manifest(
        request, user=key_user or request.user, api_key=api_key
    )

    if not _wants_html(request):
        return JsonResponse(manifest, json_dumps_params={"indent": 2, "ensure_ascii": False})

    base_url = manifest["service"]["base_url"]
    mcp = manifest["transports"]["mcp"]
    rest = manifest["transports"]["rest"]
    context = {
        "manifest": manifest,
        "auth": manifest["authentication"],
        "base_url": base_url,
        "caller": manifest["current_caller"],
        "endpoints": rest["endpoints"],
        "mcp": mcp,
        "rest_notes": rest,
        "domain": manifest["domain_model"],
        "settings": manifest["runtime_settings"],
        "tool_count": mcp["tool_count"],
        "tools": [
            {**tool, "schema_json": _pretty(tool["inputSchema"])} for tool in mcp["tools"]
        ],
        "client_configs": [
            {**entry, "config_json": _pretty(entry["config"])}
            for entry in manifest["transports"]["client_configs"]
        ],
        "errors": manifest["errors"],
        "guardrails": manifest["guardrails"],
        "curl_manifest": _curl(base_url, "/llm", api_key=False),
        "curl_workspaces": _curl(base_url, f"{API_BASE}/workspaces/"),
        "curl_projects": _curl(base_url, f"{API_BASE}/projects/"),
        "curl_search": _curl(base_url, f"{API_BASE}/search/?q=keres%C3%A9s"),
        "mcp_call": _pretty(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {"name": "knowledge_search", "arguments": {"query": "onboarding"}},
            }
        ),
    }
    return render(request, "llm_manifest.html", context)