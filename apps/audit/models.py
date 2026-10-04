import uuid

from django.conf import settings
from django.db import models


class AuditAction(models.TextChoices):
    READ = "read", "Read"
    CREATE = "create", "Create"
    UPDATE = "update", "Update"
    DELETE = "delete", "Delete"
    SEARCH = "search", "Search"
    FOLLOW_LINK = "follow_link", "Follow link"
    LOGIN = "login", "Login"
    LOGOUT = "logout", "Logout"
    CHANGE_PERMISSION = "change_permission", "Change permission"
    CREATE_API_KEY = "create_api_key", "Create API key"
    REVOKE_API_KEY = "revoke_api_key", "Revoke API key"
    USE_SECRET = "use_secret", "Use secret"
    GIT_COMMIT = "git_commit", "Git commit"
    GIT_PULL = "git_pull", "Git pull"
    GIT_PUSH = "git_push", "Git push"
    MCP_REQUEST = "mcp_request", "MCP request"


class AuditResult(models.TextChoices):
    SUCCESS = "success", "Success"
    FAILURE = "failure", "Failure"
    DENIED = "denied", "Denied"


class AuditSource(models.TextChoices):
    WEB = "web", "Web UI"
    API = "api", "REST API"
    MCP = "mcp", "MCP"
    GIT = "git", "Git"
    SYSTEM = "system", "System"


class AuditEvent(models.Model):
    """Append-only audit trail. Never stores secret values."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    timestamp = models.DateTimeField(auto_now_add=True, db_index=True)
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="audit_events",
    )
    api_key = models.ForeignKey(
        "accounts.ApiKey",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="audit_events",
    )
    action = models.CharField(max_length=32, choices=AuditAction.choices, db_index=True)
    resource = models.ForeignKey(
        "resources.Resource",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="audit_events",
    )
    workspace = models.ForeignKey(
        "workspaces.Workspace",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
    )
    project = models.ForeignKey(
        "resources.Resource",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
        help_text=(
            "The Resource of the project the event happened in. It points at the "
            "Resource and not at a Project row because an audit entry records "
            "where something happened at the time it happened: the UUID is the "
            "same one a Project row used to carry (Project.pk == its Resource "
            "id), so existing rows are untouched, and a project renamed into a "
            "plain folder - or deleted entirely - must not take history with it."
        ),
    )
    source = models.CharField(max_length=16, choices=AuditSource.choices, default=AuditSource.SYSTEM)
    ip_address = models.GenericIPAddressField(null=True, blank=True)
    user_agent = models.CharField(max_length=512, blank=True)
    result = models.CharField(max_length=16, choices=AuditResult.choices, default=AuditResult.SUCCESS)
    version = models.IntegerField(null=True, blank=True)
    git_commit = models.CharField(max_length=64, blank=True)
    detail = models.JSONField(default=dict, blank=True)

    class Meta:
        db_table = "audit_event"
        ordering = ["-timestamp"]
        indexes = [models.Index(fields=["action", "-timestamp"])]

    def __str__(self) -> str:
        return f"{self.timestamp:%Y-%m-%d %H:%M} {self.action} {self.result}"

    def save(self, *args, **kwargs):
        if not self._state.adding:
            raise ValueError("AuditEvent is append-only and cannot be modified.")
        return super().save(*args, **kwargs)
