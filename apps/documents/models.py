import uuid

from django.conf import settings
from django.db import models
from django.db.models import Q

from apps.tags.models import Taggable


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


class Document(Taggable, models.Model):
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
    #: The folder this document lives in, if any. The tree is the source of
    #: truth; ``path`` stays as a denormalised, human-readable cache of it.
    folder = models.ForeignKey(
        "documents.DocumentFolder",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="documents",
    )
    summary = models.TextField(blank=True)
    mime_type = models.CharField(max_length=128, default="text/markdown")
    is_template = models.BooleanField(
        default=False,
        help_text="Sablon: nem kerül a keresési indexbe, de új dokumentumok "
        "példányosíthatók belőle.",
    )
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


class DocumentFolder(Taggable, models.Model):
    """A named node in the knowledge tree.

    Folders were once an identity-free path segment: a row with a ``path``
    string and nothing else, which meant a folder could not be renamed without
    rewriting the path of every descendant, and could not be described or
    owned. A project and a folder then looked identical in the interface while
    only one of them was a real object.

    It is a real object now. ``container`` points at the enclosing node - another
    folder, a project, a workspace, whatever exists - so nesting is not
    constrained to "inside a project" or two levels. ``name`` is this node's own
    name, and the full ``path`` is a **denormalised cache** of the chain, kept
    because every list view sorts and filters on it. ``recompute_path()`` is the
    only thing that should write it.

    A folder still carries no content of its own: the bytes live in the files on
    disk and the knowledge lives in documents.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    resource = models.OneToOneField(
        "resources.Resource",
        on_delete=models.CASCADE,
        related_name="folder",
    )
    #: The enclosing node. Nullable only for a folder whose container row was
    #: deleted; a live folder always has one.
    container = models.ForeignKey(
        "resources.Resource",
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="child_folders",
    )
    name = models.CharField(max_length=255)
    description = models.TextField(blank=True)
    workspace = models.ForeignKey(
        "workspaces.Workspace", on_delete=models.CASCADE, related_name="folders"
    )
    project = models.ForeignKey(
        "workspaces.Project",
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="folders",
    )
    #: Denormalised chain of names, '/' separated. Derived from ``container``;
    #: never the source of truth.
    path = models.CharField(max_length=1024, blank=True)
    owner = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="owned_folders",
    )
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="created_folders",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "documents_folder"
        ordering = ["path"]
        constraints = [
            models.UniqueConstraint(
                fields=["container", "name"], name="uniq_folder_name_per_container"
            )
        ]

    def __str__(self) -> str:
        return self.path or self.name

    @property
    def parent_path(self) -> str:
        """The path of the enclosing folder, empty for a top-level folder."""
        return self.path.rsplit("/", 1)[0] if "/" in self.path else ""

    def chain(self) -> list["DocumentFolder"]:
        """This folder and its enclosing folders, nearest first."""
        out: list[DocumentFolder] = [self]
        seen = {self.id}
        container_id = self.container_id
        while container_id is not None:
            parent = (
                DocumentFolder.objects.select_related("container").filter(
                    resource_id=container_id
                ).first()
            )
            if parent is None or parent.id in seen:
                break
            seen.add(parent.id)
            out.append(parent)
            container_id = parent.container_id
        return out

    def scope_path(self) -> str:
        """Always empty: a path is relative to the workspace *or* project it is in.

        The container already knows which, so there is nothing to prefix. Adding
        the project name here would break every caller that filters a path within
        a scope - and the on-disk layout nests projects in their own directory
        anyway.
        """
        return ""

    def recompute_path(self, *, save: bool = True) -> str:
        """Rewrite ``path`` from the container chain. The only writer of it."""
        parts = [self.name]
        for parent in self.chain()[1:]:
            parts.append(parent.name)
        parts.reverse()
        derived = "/".join(parts)
        if derived != self.path:
            self.path = derived
            if save:
                self.save(update_fields=["path", "updated_at"])
        return self.path


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
