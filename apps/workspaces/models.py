from django.conf import settings
from django.db import models


class Workspace(models.Model):
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
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="created_workspaces",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "workspaces_workspace"
        ordering = ["name"]

    def __str__(self) -> str:
        return self.name

    @property
    def projects(self):
        return Project.objects.filter(workspace=self)


class Project(models.Model):
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
