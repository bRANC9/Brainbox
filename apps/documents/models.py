import uuid

from django.conf import settings
from django.db import models
from django.db.models import Q


class DocumentStatus(models.TextChoices):
    DRAFT = "draft", "Draft"
    EXPERIMENTAL = "experimental", "Experimental"
    APPROVED = "approved", "Approved"
    DEPRECATED = "deprecated", "Deprecated"
    ARCHIVED = "archived", "Archived"


class ChangeType(models.TextChoices):
    CREATE = "create", "Create"
    UPDATE = "update", "Update"
    RESTORE = "restore", "Restore"
    IMPORT = "import", "Import"
    AI = "ai", "AI generated"
    DELETE = "delete", "Delete"


class ChangeSource(models.TextChoices):
    WEB = "web", "Web UI"
    API = "api", "REST API"
    MCP = "mcp", "MCP"
    GIT = "git", "Git"
    IMPORT = "import", "Import"
    SYSTEM = "system", "System"


class Document(models.Model):
    """Markdown/knowledge document. Content lives on disk; DB holds metadata."""

    resource = models.OneToOneField(
        "resources.Resource",
        on_delete=models.CASCADE,
        primary_key=True,
        related_name="document",
    )
    workspace = models.ForeignKey(
        "workspaces.Workspace", on_delete=models.CASCADE, related_name="documents"
    )
    project = models.ForeignKey(
        "workspaces.Project",
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="documents",
    )
    title = models.CharField(max_length=500)
    slug = models.SlugField(max_length=255)
    path = models.CharField(max_length=1024, help_text="Relative path under the documents dir")
    summary = models.TextField(blank=True)
    mime_type = models.CharField(max_length=128, default="text/markdown")
    status = models.CharField(
        max_length=16, choices=DocumentStatus.choices, default=DocumentStatus.DRAFT
    )
    priority = models.IntegerField(default=0)
    frontmatter = models.JSONField(default=dict, blank=True)
    metadata = models.JSONField(default=dict, blank=True)
    current_version = models.IntegerField(default=0)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="created_documents",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "documents_document"
        ordering = ["-updated_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["workspace", "path"],
                condition=Q(project__isnull=True),
                name="uniq_document_path_workspace",
            ),
            models.UniqueConstraint(
                fields=["project", "path"],
                condition=Q(project__isnull=False),
                name="uniq_document_path_project",
            ),
        ]

    def __str__(self) -> str:
        return self.title


class DocumentVersion(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    document = models.ForeignKey(
        Document, on_delete=models.CASCADE, related_name="versions"
    )
    version = models.IntegerField()
    content = models.TextField(blank=True)
    summary = models.TextField(blank=True)
    metadata = models.JSONField(default=dict, blank=True)
    change_type = models.CharField(
        max_length=16, choices=ChangeType.choices, default=ChangeType.UPDATE
    )
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="created_document_versions",
    )
    api_key = models.ForeignKey(
        "accounts.ApiKey",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="created_document_versions",
    )
    source = models.CharField(
        max_length=16, choices=ChangeSource.choices, default=ChangeSource.WEB
    )
    git_commit = models.CharField(max_length=64, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "documents_document_version"
        ordering = ["-version"]
        constraints = [
            models.UniqueConstraint(
                fields=["document", "version"], name="uniq_document_version"
            )
        ]

    def __str__(self) -> str:
        return f"{self.document_id} v{self.version} ({self.change_type})"
