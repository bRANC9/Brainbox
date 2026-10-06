"""Gateway application service.

One rule: a caller is authorised on the target's ``Resource`` (``Permission.USE``),
never on the wire. The credential is resolved from the vault and injected
server-side, and the response is redacted before it is returned or logged.
"""

from __future__ import annotations

import base64
import http.client
import ipaddress
import json as _json
import socket
import urllib.error
import urllib.request
from datetime import timedelta
from urllib.parse import urlparse

from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.utils import timezone

from apps.audit.models import AuditAction, AuditEvent, AuditResult, AuditSource
from apps.audit.services import AuditService
from apps.permissions.constants import Effect, Permission, SubjectType
from apps.permissions.services import PermissionService
from apps.resources.models import ResourceType
from apps.resources.services import ResourceService
from apps.secrets.services import SecretService

from .models import GatewayTarget, validate_config

# A buffered response is held in memory, so it is bounded; a target may raise the
# bound up to the hard limit, and anything genuinely large goes through `stream`,
# which never buffers the whole body.
DEFAULT_MAX_RESPONSE_BYTES = 1024 * 1024
MAX_ALLOWED_RESPONSE_BYTES = 20 * 1024 * 1024
STREAM_CHUNK_BYTES = 65536
TIMEOUT_SECONDS = 20


def parse_sse_json(body: str) -> list:
    """The JSON payloads in a Server-Sent Events body.

    The MCP Streamable HTTP transport may answer a POST with ``text/event-stream``
    instead of ``application/json``: one JSON-RPC message per ``data:`` line
    group. Anything that is not JSON (a heartbeat, ``[DONE]``) is skipped.
    """
    messages = []
    for block in (body or "").split("\n\n"):
        data_lines = [
            line[5:].strip() for line in block.splitlines() if line.startswith("data:")
        ]
        if not data_lines:
            continue
        payload = "\n".join(data_lines).strip()
        if not payload or payload == "[DONE]":
            continue
        try:
            messages.append(_json.loads(payload))
        except ValueError:
            continue
    return messages


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Refuse redirects: a 302 to another host would escape the allowlist."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise urllib.error.HTTPError(newurl, code, "redirect blocked", headers, fp)


def _audit_source(request, source):
    if source is not None:
        return source
    return AuditSource.API if request is not None else AuditSource.SYSTEM


class RateLimited(Exception):
    """A target's own rate limit is reached. Not a permission fault, so the REST
    surface maps it to 429, not 403."""

    def __init__(self, retry_after: int):
        self.retry_after = retry_after
        super().__init__(
            f"Túl sok kérés ehhez a célhoz; próbáld újra {retry_after} másodperc múlva."
        )


class GatewayService:
    @classmethod
    @transaction.atomic
    def create(
        cls,
        *,
        name,
        base_url,
        kind: str = "http",
        config=None,
        secret=None,
        workspace=None,
        project=None,
        created_by=None,
        request=None,
    ) -> GatewayTarget:
        """Create a target, its Resource, and the creator's own ACL.

        The resource is parented under the workspace/project when there is one,
        so scope-based grants reach it. The creator also gets an explicit ADMIN,
        which is what makes it usable as a personal egress config with no
        workspace behind it.
        """
        validate_config(config or {})

        parent = None
        if project is not None:
            parent = project.resource
        elif workspace is not None:
            parent = workspace.resource

        resource = ResourceService.create(
            resource_type=ResourceType.GATEWAY,
            name=name,
            created_by=created_by,
            parent=parent,
        )
        target = GatewayTarget.objects.create(
            resource=resource,
            name=name,
            kind=kind,
            base_url=base_url,
            secret=secret,
            config=config or {},
            owner=created_by,
            workspace=workspace,
            project=project,
            created_by=created_by,
        )
        if secret is not None and (workspace is not None or project is not None):
            SecretService.attach(
                secret=secret, workspace=workspace, project=project, created_by=created_by
            )
        if created_by is not None:
            PermissionService.grant_unchecked(
                resource,
                subject_type=SubjectType.USER,
                subject_id=created_by.id,
                permission=Permission.ADMIN,
                effect=Effect.ALLOW,
                created_by=created_by,
            )
        AuditService.log(
            AuditAction.CREATE,
            user=created_by,
            resource=resource,
            source=AuditSource.API if request is not None else AuditSource.SYSTEM,
            request=request,
            detail={"type": "gateway_target", "kind": kind, "base_url": base_url},
        )
        return target

    @classmethod
    def visible(cls, user, *, api_key=None) -> list[GatewayTarget]:
        """The targets the caller may use, permission-filtered."""
        targets = list(
            GatewayTarget.objects.select_related("resource", "workspace", "project")
        )
        return [
            target
            for target in targets
            if PermissionService.check(user, target.resource, Permission.USE, api_key=api_key)
        ]

    @classmethod
    def resolve(cls, value) -> GatewayTarget:
        queryset = GatewayTarget.objects.select_related("resource")
        try:
            return queryset.get(pk=value)
        except (GatewayTarget.DoesNotExist, ValidationError, ValueError, TypeError):
            pass
        target = queryset.filter(name=str(value)).first()
        if target is None:
            raise ValidationError({"target": f"Nincs ilyen gateway cél: {value}"})
        return target

    @classmethod
    def call(
        cls,
        target: GatewayTarget,
        *,
        method: str = "GET",
        path: str = "",
        body=None,
        headers: dict | None = None,
        user,
        request=None,
        api_key=None,
        source=None,
    ) -> dict:
        method = cls._authorise(
            target, method, path, user, request=request, api_key=api_key, source=source
        )
        url = cls._build_url(target, path)
        secret_value = cls._secret_value(target, request=request, api_key=api_key)
        request_headers = cls._request_headers(target, headers, secret_value)
        status, response_headers, response_body, truncated = cls._send(
            method, url, request_headers, body, cls._max_bytes(target)
        )
        if secret_value:
            response_body = response_body.replace(secret_value, "***")
        cls._audit_call(target, method, path, status, user, request, api_key, source)
        return {
            "status": status,
            "headers": response_headers,
            "body": response_body,
            "truncated": truncated,
        }

    @classmethod
    def stream(
        cls,
        target: GatewayTarget,
        *,
        method: str = "GET",
        path: str = "",
        body=None,
        headers: dict | None = None,
        user,
        request=None,
        api_key=None,
        source=None,
    ):
        """Forward the upstream body as a stream: ``(status, headers, chunks)``.

        For a response too large to buffer, at the cost of not being able to
        redact: a target that injects a credential must not stream, because a
        secret could come back inside the body and there is no buffered copy to
        clean. So streaming is allowed only when the target is explicitly
        ``allow_stream`` and carries no secret.
        """
        method = cls._authorise(
            target, method, path, user, request=request, api_key=api_key, source=source
        )
        if not target.config.get("allow_stream"):
            raise ValidationError(
                {"stream": "A streamelés ehhez a célhoz nincs engedélyezve (allow_stream)."}
            )
        if target.secret_id:
            raise ValidationError(
                {
                    "stream": "Secretet injektáló cél nem streamelhető "
                    "(a választ nem lehet redaktálni)."
                }
            )
        url = cls._build_url(target, path)
        request_headers = cls._request_headers(target, headers, None)
        status, response_headers, chunks = cls._open_stream(
            method, url, request_headers, body
        )
        cls._audit_call(
            target, method, path, status, user, request, api_key, source, stream=True
        )
        return status, response_headers, chunks

    @classmethod
    def _authorise(cls, target, method, path, user, *, request, api_key, source) -> str:
        """Enabled + USE + allowlists + rate limit, or raise. Returns the method."""
        if not target.enabled:
            raise ValidationError({"target": "Ez a gateway cél ki van kapcsolva."})
        if not PermissionService.check(
            user, target.resource, Permission.USE, api_key=api_key
        ):
            AuditService.log(
                AuditAction.GATEWAY_CALL,
                user=user,
                api_key=api_key,
                resource=target.resource,
                result=AuditResult.DENIED,
                source=_audit_source(request, source),
                request=request,
                detail={"target": target.name, "method": method, "path": path},
            )
            raise PermissionDenied("Nincs jogosultságod ehhez a gateway célhoz.")

        method = (method or "GET").upper()
        cls._validate(target, method, path)
        retry_after = cls._rate_limited(target)
        if retry_after is not None:
            AuditService.log(
                AuditAction.GATEWAY_CALL,
                user=user,
                api_key=api_key,
                resource=target.resource,
                result=AuditResult.DENIED,
                source=_audit_source(request, source),
                request=request,
                detail={"target": target.name, "reason": "rate_limited"},
            )
            raise RateLimited(retry_after)
        return method

    @staticmethod
    def _audit_call(target, method, path, status, user, request, api_key, source, *, stream=False):
        AuditService.log(
            AuditAction.GATEWAY_CALL,
            user=user,
            api_key=api_key,
            resource=target.resource,
            result=AuditResult.SUCCESS,
            source=_audit_source(request, source),
            request=request,
            detail={
                "target": target.name,
                "method": method,
                "path": path,
                "status": status,
                **({"stream": True} if stream else {}),
            },
        )

    @staticmethod
    def _max_bytes(target: GatewayTarget) -> int:
        configured = target.config.get("max_response_bytes")
        if configured:
            return min(int(configured), MAX_ALLOWED_RESPONSE_BYTES)
        return DEFAULT_MAX_RESPONSE_BYTES

    # -- validation ----------------------------------------------------------
    @classmethod
    def _validate(cls, target: GatewayTarget, method: str, path: str) -> None:
        if method not in target.allow_methods():
            raise ValidationError({"method": f"Nem engedélyezett metódus: {method}"})

        host = (urlparse(target.base_url).hostname or "").lower()
        if host not in target.allow_hosts():
            # A base_url whose own host is not allowed would be a configuration
            # mistake, not a request rejection; still refuse it.
            raise ValidationError({"host": f"Nem engedélyezett host: {host}"})

        prefixes = target.allow_path_prefixes()
        clean_path = "/" + (path or "").lstrip("/")
        if prefixes and not any(clean_path.startswith(p) for p in prefixes):
            raise ValidationError({"path": "Ez az útvonal nincs engedélyezve."})

        if not target.allow_private() and cls._is_private_host(host):
            raise ValidationError({"host": "Belső/privát cím, és nincs engedélyezve."})

    @staticmethod
    def _rate_limited(target: GatewayTarget) -> int | None:
        """Seconds to wait when the target is over its limit, else None.

        Counted from the audit trail rather than an in-process counter: it is the
        same fact at any worker, and it survives a restart. Only completed calls
        count, so a denied attempt does not make the limit stricter.
        """
        limit = target.config.get("rate_limit")
        if not limit:
            return None
        window = int(target.config.get("rate_window_seconds") or 60)
        since = timezone.now() - timedelta(seconds=window)
        used = AuditEvent.objects.filter(
            resource=target.resource,
            action=AuditAction.GATEWAY_CALL,
            result=AuditResult.SUCCESS,
            timestamp__gte=since,
        ).count()
        return window if used >= int(limit) else None

    @staticmethod
    def _is_private_host(host: str) -> bool:
        try:
            infos = socket.getaddrinfo(host, None)
        except socket.gaierror:
            return False
        for info in infos:
            address = info[4][0]
            try:
                ip = ipaddress.ip_address(address)
            except ValueError:
                continue
            if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved:
                return True
        return False

    @staticmethod
    def _build_url(target: GatewayTarget, path: str) -> str:
        base = target.base_url.rstrip("/")
        clean = (path or "").lstrip("/")
        return f"{base}/{clean}" if clean else base

    # -- credential + headers ------------------------------------------------
    @classmethod
    def _secret_value(cls, target: GatewayTarget, *, request, api_key) -> str | None:
        if target.secret is None:
            return None
        # Resolved as the target's owner: the credential belongs to the mount, not
        # to whoever calls it. The caller's authorisation is the ACL above, and
        # the call itself is audited separately.
        return SecretService.reveal(
            target.secret,
            user=target.owner,
            workspace_id=target.workspace_id,
            project_id=target.project_id,
            request=request,
            api_key=api_key,
        )

    @classmethod
    def _request_headers(
        cls, target: GatewayTarget, headers: dict | None, secret_value: str | None
    ) -> dict:
        out = {str(key): str(value) for key, value in (target.config.get("headers") or {}).items()}
        for key, value in (headers or {}).items():
            # Never let a caller override an injected credential header.
            if key.lower() in {"authorization", "x-api-key", "cookie"}:
                continue
            out[str(key)] = str(value)
        out.setdefault("Accept", "application/json")

        auth = (target.config.get("auth") or "none").lower()
        if secret_value and auth != "none":
            if auth == "bearer":
                out["Authorization"] = f"Bearer {secret_value}"
            elif auth == "header":
                out[str(target.config.get("header_name") or "X-Api-Key")] = secret_value
            elif auth == "basic":
                token = base64.b64encode(f":{secret_value}".encode("utf-8")).decode("ascii")
                out["Authorization"] = f"Basic {token}"
            elif auth == "query":
                # Query injection is handled in `_send` via the URL, but a secret
                # in a URL is logged everywhere; refuse it rather than leak.
                raise ValidationError(
                    {"auth": "A 'query' auth nem támogatott (a secret a logba kerülne)."}
                )
        return out

    # -- transport -----------------------------------------------------------
    @staticmethod
    def _body_bytes(body) -> bytes | None:
        if body is None:
            return None
        if isinstance(body, str):
            return body.encode("utf-8")
        return _json.dumps(body).encode("utf-8")

    @classmethod
    def _send(cls, method, url, headers, body, max_bytes):
        req = urllib.request.Request(
            url, data=cls._body_bytes(body), method=method, headers=headers
        )
        opener = urllib.request.build_opener(_NoRedirect())
        try:
            with opener.open(req, timeout=TIMEOUT_SECONDS) as response:
                response_body, truncated = cls._read_capped(response, max_bytes)
                return response.status, cls._response_headers(response), response_body, truncated
        except urllib.error.HTTPError as exc:
            raw = exc.read() if hasattr(exc, "read") else b""
            response_body, truncated = cls._decode(raw, max_bytes)
            return exc.code, cls._response_headers(exc), response_body, truncated
        except urllib.error.URLError as exc:
            raise ValidationError(
                {"detail": f"A külső hívás nem sikerült: {exc.reason}"}
            ) from exc

    @classmethod
    def _open_stream(cls, method, url, headers, body):
        """Open the upstream connection and return ``(status, headers, chunks)``.

        Uses ``http.client`` directly (not urllib) so the body can be read in
        chunks instead of buffered, and so no redirect is followed. The generator
        owns the connection and closes it when the client stops reading.
        """
        parsed = urlparse(url)
        connection_class = (
            http.client.HTTPSConnection if parsed.scheme == "https" else http.client.HTTPConnection
        )
        connection = connection_class(parsed.hostname, parsed.port, timeout=TIMEOUT_SECONDS)
        target_path = parsed.path or "/"
        if parsed.query:
            target_path = f"{target_path}?{parsed.query}"
        try:
            connection.request(
                method, target_path, body=cls._body_bytes(body), headers=headers
            )
            response = connection.getresponse()
        except OSError as exc:
            connection.close()
            raise ValidationError(
                {"detail": f"A külső hívás nem sikerült: {exc}"}
            ) from exc

        def chunks():
            try:
                while True:
                    chunk = response.read(STREAM_CHUNK_BYTES)
                    if not chunk:
                        break
                    yield chunk
            finally:
                connection.close()

        return response.status, cls._response_headers(response), chunks()

    @classmethod
    def _read_capped(cls, stream, max_bytes) -> tuple[str, bool]:
        raw = stream.read(max_bytes + 1)
        truncated = len(raw) > max_bytes
        return cls._decode(raw[:max_bytes], max_bytes)[0], truncated

    @staticmethod
    def _decode(raw: bytes, max_bytes: int) -> tuple[str, bool]:
        truncated = len(raw) > max_bytes
        raw = raw[:max_bytes]
        try:
            return raw.decode("utf-8"), truncated
        except UnicodeDecodeError:
            return raw.decode("latin-1", "replace"), truncated

    @staticmethod
    def _response_headers(response) -> dict:
        headers = dict(getattr(response, "headers", {}) or {})
        # A cookie from an upstream service must not be handed back to the client.
        headers.pop("Set-Cookie", None)
        return {key: value for key, value in headers.items() if key.lower() != "set-cookie"}
