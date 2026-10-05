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
from django.db import models


class GatewayKind(models.TextChoices):
    HTTP = "http", "HTTP API"
    MCP = "mcp", "MCP server"
    GITHUB = "github", "GitHub"


class GatewayTarget(models.Model):
    """A named external endpoint plus the credential and rules to reach it.

    ``config`` carries the behaviour that does not deserve a column:

    * ``auth``: ``none`` (default), ``bearer``, ``header``, ``basic`` or ``query``
    * ``header_name`` / ``query_name``: where the credential goes for those schemes
    * ``headers``: extra headers injected on every call
    * ``allow_hosts``: host allowlist; defaults to the host of ``base_url``
    * ``allow_methods``: defaults to ``["GET", "POST"]``
    * ``allow_path_prefixes``: optional prefix allowlist
    * ``allow_private``: permit a private/loopback host (internal services)
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
