from django.conf import settings
from django.db import models
from django.db.models import Q

from apps.tags.models import Taggable


class WorkspaceKind(models.TextChoices):
    """What kind of audience a workspace serves.

    Not a taxonomy of subject matter (there deliberately is none - see
    ``terv.md``), but a statement about *audience*: ``SHARED`` workspaces start
    private and are opened up by granting access, ``PERSONAL`` workspaces are a
    per-user home that is never shareable.
    """

    SHARED = "shared", "Shared"
    PERSONAL = "personal", "Personal"


class Workspace(Taggable, models.Model):
    """Top level container. Its Resource doubles as its primary key."""

    resource = models.OneToOneField(
        "resources.Resource",
        on_delete=models.CASCADE,
        primary_key=True,
        related_name="workspace",
    )
    name = models.CharField(max_length=255)
    slug = models.SlugField(max_length=255, unique=True)
    description = models.TextField(blank=True)
    kind = models.CharField(
        max_length=16, choices=WorkspaceKind.choices, default=WorkspaceKind.SHARED
    )
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="created_workspaces",
    )
    owner = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="owned_workspaces",
        help_text="A workspace tulajdonosa: egyedül ő módosíthatja a workspace "
        "ACL-jét, és ő adhat át tulajdont. NULL = tulajdon nélküli (örökség) sor, "
        "amelynek ACL-jét csak auditált superuser-átvétellel lehet elérni.",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "workspaces_workspace"
        ordering = ["name"]
        constraints = [
            models.UniqueConstraint(
                fields=["owner"], condition=Q(kind="personal"), name="uniq_personal_per_owner"
            )
        ]

    def __str__(self) -> str:
        return self.name

    @property
    def projects(self):
        return Project.objects.filter(workspace=self)

    @property
    def is_personal(self) -> bool:
        return self.kind == WorkspaceKind.PERSONAL


class Project(Taggable, models.Model):
    """A workspace-scoped container with its own Resource and ACL."""

    resource = models.OneToOneField(
        "resources.Resource",
        on_delete=models.CASCADE,
        primary_key=True,
        related_name="project",
    )
    workspace = models.ForeignKey(
        Workspace, on_delete=models.CASCADE, related_name="project_set"
    )
    name = models.CharField(max_length=255)
    slug = models.SlugField(max_length=255)
    description = models.TextField(blank=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="created_projects",
    )
    owner = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="owned_projects",
        help_text="A projekt tulajdonosa. A létrehozó az alapértelmezett; "
        "a tulajdon transferálható, de csak a workspace/projekt tulajdonosa "
        "vagy auditált superuser-átvétel után.",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "workspaces_project"
        ordering = ["name"]
        constraints = [
            models.UniqueConstraint(
                fields=["workspace", "slug"], name="uniq_project_slug_per_workspace"
            )
        ]

    def __str__(self) -> str:
        return f"{self.workspace.name}/{self.name}"
