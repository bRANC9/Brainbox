"""Read-only admin for files and file versions.

Write path: :class:`~apps.files.services.FileService`, which stores the bytes,
appends a :class:`~apps.files.models.FileVersion` and maintains the checksum.
"""

from django.contrib import admin

from apps.resources.admin import ResourceBackedAdminMixin, ResourceScopedAdminMixin

from .models import File, FileVersion


@admin.register(File)
class FileAdmin(ResourceBackedAdminMixin, admin.ModelAdmin):
    list_display = ("name", "workspace", "project", "mime_type", "size", "updated_at")
    list_filter = ("workspace", "mime_type")
    search_fields = ("name", "path")
    # `checksum` and `current_version` describe the bytes on disk; editing either
    # here would make the metadata lie about the file. `permission_link` is the
    # one way out of a read-only page.
    readonly_fields = (
        "resource",
        "permission_link",
        "checksum",
        "current_version",
        "created_by",
        "created_at",
        "updated_at",
    )


@admin.register(FileVersion)
class FileVersionAdmin(ResourceScopedAdminMixin, admin.ModelAdmin):
    resource_id_fields = ("file__resource_id",)
    list_display = ("file", "version", "size", "checksum", "created_by", "created_at")
    search_fields = ("file__name",)
    readonly_fields = [field.name for field in FileVersion._meta.fields]
