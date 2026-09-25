import uuid

from django.conf import settings
from django.db import models
from django.db.models import Q

from apps.documents.models import ChangeSource, ChangeType


class File(models.Model):
    """Arbitrary binary/text file. Bytes live on disk, metadata in the DB."""

    resource = models.OneToOneField(
        "resources.Resource",
        on_delete=models.CASCADE,
        primary_key=True,
        related_name="file",
    )
    workspace = models.ForeignKey(
        "workspaces.Workspace", on_delete=models.CASCADE, related_name="files"
    )
    project = models.ForeignKey(
        "workspaces.Project",
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="files",
    )
    name = models.CharField(max_length=512)
    path = models.CharField(max_length=1024)
    mime_type = models.CharField(max_length=255, default="application/octet-stream")
    size = models.BigIntegerField(default=0)
    checksum = models.CharField(max_length=64, blank=True)
    metadata = models.JSONField(default=dict, blank=True)
    current_version = models.IntegerField(default=1)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="created_files",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "files_file"
        ordering = ["-updated_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["workspace", "path"],
                condition=Q(project__isnull=True),
                name="uniq_file_path_workspace",
            ),
            models.UniqueConstraint(
                fields=["project", "path"],
                condition=Q(project__isnull=False),
                name="uniq_file_path_project",
            ),
        ]

    def __str__(self) -> str:
        return self.name


class FileVersion(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    file = models.ForeignKey(File, on_delete=models.CASCADE, related_name="versions")
    version = models.IntegerField()
    size = models.BigIntegerField(default=0)
    checksum = models.CharField(max_length=64, blank=True)
    storage_path = models.CharField(max_length=1024)
    change_type = models.CharField(
        max_length=16, choices=ChangeType.choices, default=ChangeType.UPDATE
    )
    source = models.CharField(
        max_length=16, choices=ChangeSource.choices, default=ChangeSource.WEB
    )
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="created_file_versions",
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "files_file_version"
        ordering = ["-version"]
        constraints = [
            models.UniqueConstraint(fields=["file", "version"], name="uniq_file_version")
        ]

    def __str__(self) -> str:
        return f"{self.file_id} v{self.version}"
