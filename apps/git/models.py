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
    secret = models.ForeignKey(
        "secrets.Secret",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="git_repositories",
        help_text="Optional Secret Vault credential used for this repository "
        "(overrides BRAINBOX_GIT_TOKEN). The secret must be owned by the user "
        "who created the repository and attached to its workspace/project.",
    )
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
        root = (
            Path(settings.BRAINBOX_GIT_ROOT)
            if settings.BRAINBOX_GIT_ROOT
            else Path(settings.KNOWLEDGE_DATA_ROOT) / "git"
        )
        return root / str(self.pk)

    @property
    def scope_label(self) -> str:
        if self.project_id:
            return f"{self.workspace.name}/{self.project.name}"
        return self.workspace.name


class GitCredential(models.Model):
    """A user's own credential for a repository (their own PAT/SSH secret).

    Resolution order when pushing: the acting user's credential here, then the
    repository-level secret, then the global BRAINBOX_GIT_TOKEN. When a shared
    credential is used, the commit gets a ``Co-authored-by`` trailer naming its
    owner, so the git history stays (half) traceable out of the box.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    repository = models.ForeignKey(
        GitRepository, on_delete=models.CASCADE, related_name="credentials"
    )
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="git_credentials"
    )
    secret = models.ForeignKey(
        "secrets.Secret", on_delete=models.CASCADE, related_name="git_credentials"
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "git_credential"
        ordering = ["user"]
        constraints = [
            models.UniqueConstraint(
                fields=["repository", "user"], name="uniq_git_credential_per_user"
            )
        ]

    def __str__(self) -> str:
        return f"{self.user} -> {self.repository_id} ({self.secret.name})"


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
