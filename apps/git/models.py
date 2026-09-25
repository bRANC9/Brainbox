import uuid
from pathlib import Path

from django.conf import settings
from django.db import models


class GitWorkflow(models.TextChoices):
    DIRECT_COMMIT = "direct_commit", "Direct commit"
    BRANCH_PR = "branch_pr", "Feature branch + PR"


class GitSyncStatus(models.TextChoices):
    IDLE = "idle", "Idle"
    SYNCING = "syncing", "Syncing"
    ERROR = "error", "Error"


class GitRepository(models.Model):
    """Git repository attached to a Workspace or Project.

    When attached, the repository checkout is the source-of-truth storage for
    the resource (see apps.resources.storage.resolve_base).
    """

    resource = models.OneToOneField(
        "resources.Resource",
        on_delete=models.CASCADE,
        primary_key=True,
        related_name="git_repository",
    )
    workspace = models.ForeignKey(
        "workspaces.Workspace",
        on_delete=models.CASCADE,
        related_name="git_repositories",
    )
    project = models.ForeignKey(
        "workspaces.Project",
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="git_repositories",
    )
    name = models.CharField(max_length=255)
    remote_url = models.CharField(max_length=1024, blank=True)
    default_branch = models.CharField(max_length=255, default="main")
    workflow = models.CharField(
        max_length=16, choices=GitWorkflow.choices, default=GitWorkflow.DIRECT_COMMIT
    )
    auto_sync = models.BooleanField(
        default=True, help_text="Commit platform edits back to Git automatically."
    )
    is_active = models.BooleanField(default=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="created_git_repositories",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "git_repository"
        ordering = ["name"]

    def __str__(self) -> str:
        scope = f"project:{self.project_id}" if self.project_id else "workspace"
        return f"{self.name} [{scope}]"

    @property
    def directory(self) -> Path:
        return Path(settings.KNOWLEDGE_DATA_ROOT) / "git" / str(self.pk)

    @property
    def scope_label(self) -> str:
        if self.project_id:
            return f"{self.workspace.name}/{self.project.name}"
        return self.workspace.name


class GitSyncState(models.Model):
    repository = models.OneToOneField(
        GitRepository, on_delete=models.CASCADE, related_name="sync_state"
    )
    status = models.CharField(
        max_length=16, choices=GitSyncStatus.choices, default=GitSyncStatus.IDLE
    )
    branch = models.CharField(max_length=255, blank=True)
    last_synced_sha = models.CharField(max_length=64, blank=True)
    last_pulled_at = models.DateTimeField(null=True, blank=True)
    last_pushed_at = models.DateTimeField(null=True, blank=True)
    last_error = models.TextField(blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "git_sync_state"

    def __str__(self) -> str:
        return f"{self.repository_id} {self.status}"


class GitCommitReference(models.Model):
    class Direction(models.TextChoices):
        IMPORT = "import", "Import (Git -> platform)"
        EXPORT = "export", "Export (platform -> Git)"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    repository = models.ForeignKey(
        GitRepository, on_delete=models.CASCADE, related_name="commits"
    )
    sha = models.CharField(max_length=64)
    branch = models.CharField(max_length=255, blank=True)
    message = models.CharField(max_length=1000, blank=True)
    author_name = models.CharField(max_length=255, blank=True)
    author_email = models.CharField(max_length=255, blank=True)
    direction = models.CharField(max_length=16, choices=Direction.choices)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="git_commits",
    )
    committed_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "git_commit_reference"
        ordering = ["-created_at"]
        indexes = [models.Index(fields=["repository", "-created_at"])]

    def __str__(self) -> str:
        return f"{self.sha[:10]} {self.message[:60]}"
