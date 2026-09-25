import uuid

from django.conf import settings
from django.db import models


class SecretType(models.TextChoices):
    PASSWORD = "password", "Username / password"
    API_KEY = "api_key", "API key"
    BEARER_TOKEN = "bearer_token", "Bearer token"
    SSH_KEY = "ssh_key", "SSH key"
    CERTIFICATE = "certificate", "Certificate"
    CLIENT_CREDENTIALS = "client_credentials", "Client ID + secret"
    JSON = "json", "JSON"
    KEY_VALUE = "key_value", "Key / value"
    GENERIC = "generic", "Generic"


class Secret(models.Model):
    """User-owned secret. The value is encrypted at rest and never audited."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    owner = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="secrets"
    )
    name = models.CharField(max_length=255)
    description = models.TextField(blank=True)
    secret_type = models.CharField(
        max_length=32, choices=SecretType.choices, default=SecretType.GENERIC
    )
    encrypted_payload = models.TextField()
    fingerprint = models.CharField(max_length=64, blank=True)
    metadata = models.JSONField(default=dict, blank=True)
    is_active = models.BooleanField(default=True)
    last_used_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "secrets_secret"
        ordering = ["name"]
        constraints = [
            models.UniqueConstraint(fields=["owner", "name"], name="uniq_owner_secret_name")
        ]

    def __str__(self) -> str:
        return f"{self.owner_id}:{self.name}"


class SecretAttachment(models.Model):
    """Grants use of a user secret in a workspace/project context."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    secret = models.ForeignKey(Secret, on_delete=models.CASCADE, related_name="attachments")
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
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="created_secret_attachments",
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "secrets_attachment"
        constraints = [
            models.UniqueConstraint(
                fields=["secret", "workspace", "project"], name="uniq_secret_attachment"
            )
        ]

    def __str__(self) -> str:
        scope = "global"
        if self.project_id:
            scope = f"project:{self.project_id}"
        elif self.workspace_id:
            scope = f"workspace:{self.workspace_id}"
        return f"{self.secret_id} -> {scope}"

    def matches(self, workspace_id=None, project_id=None) -> bool:
        if self.workspace_id is None and self.project_id is None:
            return True
        if self.project_id is not None and project_id is not None:
            return str(self.project_id) == str(project_id)
        if self.workspace_id is not None and workspace_id is not None:
            return str(self.workspace_id) == str(workspace_id)
        return False
