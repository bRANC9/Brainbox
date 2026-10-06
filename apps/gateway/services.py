"""Gateway application service.

One rule: a caller is authorised on the target's ``Resource`` (``Permission.USE``),
never on the wire. The credential is resolved from the vault and injected
server-side, and the response is redacted before it is returned or logged.
"""

from __future__ import annotations

import base64
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

MAX_RESPONSE_BYTES = 262144
TIMEOUT_SECONDS = 20


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
        url = cls._build_url(target, path)
        secret_value = cls._secret_value(target, request=request, api_key=api_key)
        request_headers = cls._request_headers(target, headers, secret_value)
        status, response_headers, response_body, truncated = cls._send(
            method, url, request_headers, body
        )
        if secret_value:
            response_body = response_body.replace(secret_value, "***")
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
            },
        )
        return {
            "status": status,
            "headers": response_headers,
            "body": response_body,
            "truncated": truncated,
        }

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
    @classmethod
    def _send(cls, method, url, headers, body):
        data = None
        if body is not None:
            data = (
                body.encode("utf-8") if isinstance(body, str) else _json.dumps(body).encode("utf-8")
            )
        req = urllib.request.Request(url, data=data, method=method, headers=headers)
        opener = urllib.request.build_opener(_NoRedirect())
        try:
            with opener.open(req, timeout=TIMEOUT_SECONDS) as response:
                response_body, truncated = cls._read_capped(response)
                return response.status, cls._response_headers(response), response_body, truncated
        except urllib.error.HTTPError as exc:
            raw = exc.read() if hasattr(exc, "read") else b""
            response_body, truncated = cls._decode(raw)
            return exc.code, cls._response_headers(exc), response_body, truncated
        except urllib.error.URLError as exc:
            raise ValidationError(
                {"detail": f"A külső hívás nem sikerült: {exc.reason}"}
            ) from exc

    @classmethod
    def _read_capped(cls, stream) -> tuple[str, bool]:
        raw = stream.read(MAX_RESPONSE_BYTES + 1)
        truncated = len(raw) > MAX_RESPONSE_BYTES
        return cls._decode(raw[:MAX_RESPONSE_BYTES])[0], truncated

    @staticmethod
    def _decode(raw: bytes) -> tuple[str, bool]:
        truncated = len(raw) > MAX_RESPONSE_BYTES
        raw = raw[:MAX_RESPONSE_BYTES]
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
