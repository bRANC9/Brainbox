"""Minimal MCP (Model Context Protocol) server over JSON-RPC 2.0.

Supports the core lifecycle plus tools: initialize, notifications/initialized,
ping, tools/list, tools/call. Transport is HTTP POST (works behind Pangolin).
"""

from __future__ import annotations

import json
import logging

from . import tools

logger = logging.getLogger("brainbox.mcp")

PROTOCOL_VERSION = "2024-11-05"
SERVER_NAME = "brainbox"
SERVER_VERSION = "1.0.0"


class MCPError(Exception):
    def __init__(self, code: int, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


class MCPServer:
    @classmethod
    def handle(cls, payload, ctx):
        """Return a JSON-RPC response dict, or ``None`` for notifications."""
        if not isinstance(payload, dict):
            raise MCPError(-32600, "Invalid Request")

        method = payload.get("method")
        if not method:
            raise MCPError(-32600, "Invalid Request")

        is_notification = "id" not in payload
        try:
            result = cls._dispatch(method, payload.get("params") or {}, ctx)
        except MCPError:
            raise
        except tools.ToolError as exc:
            result = {"content": [{"type": "text", "text": str(exc)}], "isError": True}
        except Exception:  # noqa: BLE001 - never leak a stack trace to the client
            logger.exception("tool execution failed")
            result = {"content": [{"type": "text", "text": "Internal error"}], "isError": True}

        if is_notification:
            return None
        return {"jsonrpc": "2.0", "id": payload.get("id"), "result": result}

    @classmethod
    def _dispatch(cls, method: str, params: dict, ctx) -> dict:
        if method == "initialize":
            return {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
            }
        if method in {"notifications/initialized", "initialized"}:
            return {}
        if method == "ping":
            return {}
        if method == "tools/list":
            return {"tools": tools.definitions()}
        if method == "tools/call":
            name = params.get("name")
            if not name:
                raise MCPError(-32602, "Missing tool name")
            arguments = params.get("arguments") or {}
            value = tools.call(name, ctx, arguments)
            text = value if isinstance(value, str) else json.dumps(value, default=str, ensure_ascii=False)
            return {"content": [{"type": "text", "text": text}], "isError": False}
        raise MCPError(-32601, f"Method not found: {method}")
