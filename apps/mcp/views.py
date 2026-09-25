"""HTTP endpoint for MCP (JSON-RPC 2.0)."""

from __future__ import annotations

import json
import logging

from django.http import JsonResponse
from django.views.decorators.csrf import csrf_exempt
from rest_framework.exceptions import AuthenticationFailed

from apps.accounts.authentication import resolve_api_key
from apps.audit.models import AuditAction, AuditSource
from apps.audit.services import AuditService

from .server import PROTOCOL_VERSION, SERVER_NAME, SERVER_VERSION, MCPError, MCPServer
from .tools import ToolContext

logger = logging.getLogger("brainbox.mcp")


def _error(code: int, message: str, rpc_id=None, status: int = 200) -> JsonResponse:
    return JsonResponse(
        {"jsonrpc": "2.0", "id": rpc_id, "error": {"code": code, "message": message}},
        status=status,
    )


@csrf_exempt
def mcp_endpoint(request):
    if request.method == "GET":
        return JsonResponse(
            {
                "name": SERVER_NAME,
                "version": SERVER_VERSION,
                "protocolVersion": PROTOCOL_VERSION,
                "transport": "http",
            }
        )
    if request.method != "POST":
        return _error(-32600, "Only GET/POST are supported.", status=405)

    try:
        user, api_key = resolve_api_key(request)
    except AuthenticationFailed as exc:
        return _error(-32001, str(exc), status=401)

    if user is None:
        if getattr(request.user, "is_authenticated", False):
            user, api_key = request.user, None
        else:
            return _error(-32001, "Authentication required.", status=401)

    try:
        payload = json.loads(request.body.decode("utf-8") or "{}")
    except (ValueError, UnicodeDecodeError):
        return _error(-32700, "Parse error")

    ctx = ToolContext(user=user, api_key=api_key, request=request)

    if isinstance(payload, list):
        responses = []
        for item in payload:
            try:
                response = MCPServer.handle(item, ctx)
            except MCPError as exc:
                response = {
                    "jsonrpc": "2.0",
                    "id": item.get("id") if isinstance(item, dict) else None,
                    "error": {"code": exc.code, "message": exc.message},
                }
            if response is not None:
                responses.append(response)
        return JsonResponse(responses, safe=False)

    rpc_id = payload.get("id") if isinstance(payload, dict) else None
    try:
        response = MCPServer.handle(payload, ctx)
    except MCPError as exc:
        return _error(exc.code, exc.message, rpc_id)
    except Exception:  # noqa: BLE001
        logger.exception("mcp internal error")
        return _error(-32603, "Internal error", rpc_id)

    if isinstance(payload, dict) and payload.get("method") == "tools/call":
        AuditService.log(
            AuditAction.MCP_REQUEST,
            user=user,
            api_key=api_key,
            source=AuditSource.MCP,
            request=request,
            detail={"tool": (payload.get("params") or {}).get("name")},
        )

    if response is None:
        return JsonResponse({}, status=202)
    return JsonResponse(response)
