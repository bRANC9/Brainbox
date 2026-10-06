"""Egress gateway: named external targets reached through Brainbox.

A client never holds the credential for GitHub, an MCP server or an internal
API. It names a target; Brainbox checks the ACL on that target's ``Resource``,
injects the secret from the vault and audits the call. One place to configure
outbound access, and one place to revoke it.
"""

from __future__ import annotations

import uuid
from urllib.parse import urlparse

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models


class GatewayKind(models.TextChoices):
    HTTP = "http", "HTTP API"
    MCP = "mcp", "MCP server"
    GITHUB = "github", "GitHub"


# The only keys `config` may carry. A typo used to fall silently through to the
# default, so a target looked configured but was not; now it is a validation
# error. `query_name` is absent on purpose: query-string auth is refused (the
# secret would land in every access log), so there is no such setting to typo.
ALLOWED_CONFIG_KEYS = {
    "auth",
    "header_name",
    "headers",
    "allow_hosts",
    "allow_methods",
    "allow_path_prefixes",
    "allow_private",
    "mcp_path",
    "rate_limit",
    "rate_window_seconds",
    "max_response_bytes",
    "allow_stream",
}
AUTH_SCHEMES = {"none", "bearer", "header", "basic"}
HTTP_METHODS = {"GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"}


def validate_config(config) -> None:
    """Validate a target's ``config``, raising ``ValidationError`` on a bad key.

    Called from the service, the serializer and the model's ``clean`` so every
    write path agrees on what a valid target is.
    """
    if config is None:
        return
    if not isinstance(config, dict):
        raise ValidationError({"config": "A config egy objektum (JSON) kell legyen."})

    unknown = set(config) - ALLOWED_CONFIG_KEYS
    if unknown:
        raise ValidationError(
            {"config": f"Ismeretlen config kulcs(ok): {', '.join(sorted(unknown))}"}
        )

    auth = config.get("auth", "none")
    if auth not in AUTH_SCHEMES:
        raise ValidationError({"config": f"Ismeretlen auth séma: {auth}"})
    if auth == "header" and not config.get("header_name"):
        raise ValidationError({"config": "A 'header' auth-hoz 'header_name' kell."})

    for key in ("allow_hosts", "allow_methods", "allow_path_prefixes"):
        if key in config and not (
            isinstance(config[key], list)
            and all(isinstance(item, str) for item in config[key])
        ):
            raise ValidationError({"config": f"A '{key}' szöveglista kell legyen."})

    if "headers" in config and not isinstance(config["headers"], dict):
        raise ValidationError({"config": "A 'headers' objektum kell legyen."})

    if "allow_methods" in config:
        for method in config["allow_methods"]:
            if method.upper() not in HTTP_METHODS:
                raise ValidationError({"config": f"Ismeretlen metódus: {method}"})

    if "allow_private" in config and not isinstance(config["allow_private"], bool):
        raise ValidationError({"config": "Az 'allow_private' logikai érték kell legyen."})

    for key in ("rate_limit", "rate_window_seconds", "max_response_bytes"):
        if key in config and not (
            isinstance(config[key], int) and not isinstance(config[key], bool) and config[key] > 0
        ):
            raise ValidationError({"config": f"A '{key}' pozitív egész kell legyen."})

    if "allow_stream" in config and not isinstance(config["allow_stream"], bool):
        raise ValidationError({"config": "Az 'allow_stream' logikai érték kell legyen."})


class GatewayTarget(models.Model):
    """A named external endpoint plus the credential and rules to reach it.

    Reaching it needs ``Permission.USE`` on its ``Resource`` - not WRITE. Using a
    target is not editing it, and USE is a separate axis from read/write, so a
    workspace member who can write does not automatically get egress.

    ``config`` carries the behaviour that does not deserve a column:

    * ``auth``: ``none`` (default), ``bearer``, ``header`` or ``basic``
    * ``header_name``: where the credential goes for the ``header`` scheme
    * ``headers``: extra headers injected on every call
    * ``allow_hosts``: host allowlist; defaults to the host of ``base_url``
    * ``allow_methods``: defaults to ``["GET", "POST"]``
    * ``allow_path_prefixes``: optional prefix allowlist
    * ``allow_private``: permit a private/loopback host (internal services)
    * ``mcp_path``: JSON-RPC path for an MCP target (default ``/mcp``)
    * ``rate_limit`` / ``rate_window_seconds``: cap the outbound calls per window
    * ``max_response_bytes``: buffered-response cap (default 1 MiB, hard max 20 MiB)
    * ``allow_stream``: allow the caller to stream the response (needs no secret)
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    resource = models.OneToOneField(
        "resources.Resource", on_delete=models.CASCADE, related_name="gateway_target"
    )
    name = models.CharField(max_length=255)
    kind = models.CharField(
        max_length=16, choices=GatewayKind.choices, default=GatewayKind.HTTP
    )
    base_url = models.URLField(max_length=2048)
    secret = models.ForeignKey(
        "secrets.Secret",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="gateway_targets",
    )
    config = models.JSONField(default=dict, blank=True)
    enabled = models.BooleanField(default=True)
    owner = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="gateway_targets",
    )
    workspace = models.ForeignKey(
        "workspaces.Workspace",
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="gateway_targets",
    )
    project = models.ForeignKey(
        "workspaces.Project",
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="gateway_targets",
    )
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="created_gateway_targets",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "gateway_target"
        ordering = ["name"]
        constraints = [
            models.UniqueConstraint(
                fields=["owner", "name"], name="uniq_owner_gateway_name"
            )
        ]

    def __str__(self) -> str:
        return f"{self.name} ({self.kind})"

    def clean(self):
        # So the admin and any full_clean() path reject a bad config too.
        validate_config(self.config)

    # -- config accessors ----------------------------------------------------
    def allow_hosts(self) -> list[str]:
        configured = self.config.get("allow_hosts")
        if configured:
            return [str(host).lower() for host in configured]
        host = (urlparse(self.base_url).hostname or "").lower()
        return [host] if host else []

    def allow_methods(self) -> list[str]:
        configured = self.config.get("allow_methods")
        if configured:
            return [str(method).upper() for method in configured]
        return ["GET", "POST"]

    def allow_path_prefixes(self) -> list[str]:
        return [str(prefix) for prefix in self.config.get("allow_path_prefixes") or []]

    def allow_private(self) -> bool:
        return bool(self.config.get("allow_private"))
