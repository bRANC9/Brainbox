import hashlib
import hmac
import secrets
import uuid

from django.conf import settings
from django.contrib.auth.models import AbstractUser
from django.db import models
from django.utils import timezone

from apps.permissions.constants import Effect
from apps.permissions.constants import Permission as PermissionChoice


class User(AbstractUser):
    """Platform user. Identity is independent of the auth provider (local/OIDC)."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    display_name = models.CharField(max_length=255, blank=True)
    oidc_subject = models.CharField(max_length=255, blank=True, default="")
    oidc_issuer = models.CharField(max_length=255, blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "accounts_user"
        ordering = ["username"]
        constraints = [
            models.UniqueConstraint(
                fields=["oidc_issuer", "oidc_subject"],
                condition=~models.Q(oidc_subject=""),
                name="uniq_oidc_identity",
            )
        ]

    def __str__(self) -> str:
        return self.display_name or self.username

    @property
    def group_ids(self) -> list[uuid.UUID]:
        from apps.groups.models import GroupMembership

        return list(
            GroupMembership.objects.filter(user=self).values_list("group_id", flat=True)
        )


class ApiKey(models.Model):
    """Per-user API/MCP key.

    The raw value is shown only once at creation time. Only an HMAC-SHA256
    digest (keyed with SECRET_KEY) and a lookup prefix are stored.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="api_keys"
    )
    name = models.CharField(max_length=255)
    key_prefix = models.CharField(max_length=16, db_index=True)
    key_hash = models.CharField(max_length=64)
    is_active = models.BooleanField(default=True)
    expires_at = models.DateTimeField(null=True, blank=True)
    last_used_at = models.DateTimeField(null=True, blank=True)
    revoked_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "accounts_api_key"
        ordering = ["-created_at"]

    def __str__(self) -> str:
        return f"{self.name} ({self.key_prefix}...)"

    # -- key material --------------------------------------------------------
    @staticmethod
    def generate_raw_key(prefix: str | None = None) -> str:
        prefix = prefix or settings.BRAINBOX_API_KEY_PREFIX
        return f"{prefix}{secrets.token_hex(24)}"

    @staticmethod
    def hash_key(raw_key: str) -> str:
        return hmac.new(
            settings.SECRET_KEY.encode(), raw_key.encode(), hashlib.sha256
        ).hexdigest()

    def set_key(self, raw_key: str) -> None:
        self.key_prefix = raw_key[:16]
        self.key_hash = self.hash_key(raw_key)

    def matches(self, raw_key: str) -> bool:
        return hmac.compare_digest(self.key_hash, self.hash_key(raw_key))

    @property
    def is_expired(self) -> bool:
        return self.expires_at is not None and self.expires_at <= timezone.now()

    @property
    def is_usable(self) -> bool:
        return self.is_active and self.revoked_at is None and not self.is_expired

    def revoke(self) -> None:
        self.is_active = False
        self.revoked_at = timezone.now()
        self.save(update_fields=["is_active", "revoked_at"])


class ApiKeyScope(models.Model):
    """Narrows a key's effective permissions. A key can never exceed its user."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    api_key = models.ForeignKey(
        ApiKey, on_delete=models.CASCADE, related_name="scopes"
    )
    workspace = models.ForeignKey(
        "workspaces.Workspace",
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="+",
    )
    project = models.ForeignKey(
        "workspaces.Project",
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="+",
    )
    permission = models.CharField(max_length=16, choices=PermissionChoice.choices)
    effect = models.CharField(max_length=8, choices=Effect.choices, default=Effect.ALLOW)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "accounts_api_key_scope"
        constraints = [
            models.UniqueConstraint(
                fields=["api_key", "workspace", "project", "permission"],
                name="uniq_api_key_scope",
            )
        ]

    def __str__(self) -> str:
        target = "global"
        if self.project_id:
            target = f"project:{self.project_id}"
        elif self.workspace_id:
            target = f"workspace:{self.workspace_id}"
        return f"{self.api_key_id} {target} {self.effect}:{self.permission}"

    def applies_to(self, workspace_resource_id, project_resource_id) -> bool:
        """Whether this scope covers the resource identified by its ancestors.

        `workspace_resource_id`/`project_resource_id` are the Resource ids of the
        workspace/project ancestors (Workspace.pk == its Resource id).
        """
        if self.workspace_id is None and self.project_id is None:
            return True
        if self.project_id is not None:
            return (
                project_resource_id is not None
                and str(project_resource_id) == str(self.project_id)
            )
        if self.workspace_id is not None:
            return (
                workspace_resource_id is not None
                and str(workspace_resource_id) == str(self.workspace_id)
            )
        return False
