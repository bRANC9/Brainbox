"""MCP resources: readable knowledge exposed as URIs.

The tools are the action surface; MCP *resources* are the read surface - what an
IDE can browse or attach to a context. Each document the caller may read becomes
``brainbox://documents/<id>``; reading it is the same permission check and the
same content path the tools use.
"""

from __future__ import annotations

from apps.documents.models import Document
from apps.documents.services import DocumentService
from apps.permissions.constants import Permission
from apps.permissions.services import PermissionService

from .errors import MCPError

URI_PREFIX = "brainbox://documents/"
PAGE_SIZE = 100


def list_resources(ctx, cursor=None) -> dict:
    try:
        offset = max(int(cursor), 0) if cursor else 0
    except (TypeError, ValueError):
        raise MCPError(-32602, "Invalid cursor") from None

    batch = list(
        Document.objects.select_related("resource")
        .order_by("title")[offset : offset + PAGE_SIZE]
    )
    resource_ids = [document.resource_id for document in batch]
    readable = set(
        PermissionService.allowed_resource_ids(
            ctx.user, resource_ids, Permission.READ, api_key=ctx.api_key
        )
    )
    resources = [
        {
            "uri": f"{URI_PREFIX}{document.pk}",
            "name": document.title,
            "description": document.path,
            "mimeType": "text/markdown",
        }
        for document in batch
        if document.resource_id in readable
    ]
    result = {"resources": resources}
    if len(batch) == PAGE_SIZE:
        result["nextCursor"] = str(offset + PAGE_SIZE)
    return result


def read_resource(ctx, uri) -> dict:
    if not isinstance(uri, str) or not uri.startswith(URI_PREFIX):
        raise MCPError(-32602, f"Unknown resource uri: {uri}")
    identifier = uri[len(URI_PREFIX) :]
    document = (
        Document.objects.select_related("resource").filter(pk=identifier).first()
    )
    # An unreadable document and a missing one answer identically, so the URI
    # space cannot be walked to learn what exists.
    if document is None or not PermissionService.check(
        ctx.user, document.resource, Permission.READ, api_key=ctx.api_key
    ):
        raise MCPError(-32602, f"Unknown resource uri: {uri}")
    return {
        "contents": [
            {
                "uri": uri,
                "mimeType": "text/markdown",
                "text": DocumentService.read_content(document),
            }
        ]
    }
