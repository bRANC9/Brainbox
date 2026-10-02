"""Read-only admin for documents, versions and folders.

Write path: :class:`~apps.documents.services.DocumentService` for the content
lifecycle (create, update, restore, move, delete) and
:func:`apps.documents.folders.create_folder` /
:func:`~apps.documents.folders.rename_folder` for the tree. Both keep the file
on disk, the version rows, the folder resources, the link graph and the
extracted deadlines in step; a ModelForm would update the metadata row and
nothing else.
"""

from django.contrib import admin

from apps.resources.admin import ResourceBackedAdminMixin, ResourceScopedAdminMixin

from .models import Document, DocumentVersion


class DocumentVersionInline(ResourceScopedAdminMixin, admin.TabularInline):
    model = DocumentVersion
    # The parent document is read-only, so the inline must be too: a "delete"
    # button here would drop a version without DocumentService running.
    resource_id_fields = ("document__resource_id",)
    fields = ("version", "change_type", "source", "created_by", "created_at")
    readonly_fields = fields
    show_change_link = False


@admin.register(Document)
class DocumentAdmin(ResourceBackedAdminMixin, admin.ModelAdmin):
    list_display = ("title", "workspace", "project", "status", "priority", "current_version", "updated_at")
    list_filter = ("status", "workspace")
    search_fields = ("title", "path", "summary")
    # `current_version` is derived from the version history, `path`/`summary`
    # are the index's view of the file - all three are written by the service.
    # `permission_link` is the one way out of a read-only page: access is
    # managed in the web UI, never here.
    readonly_fields = (
        "resource",
        "permission_link",
        "current_version",
        "created_by",
        "created_at",
        "updated_at",
    )
    inlines = [DocumentVersionInline]


@admin.register(DocumentVersion)
class DocumentVersionAdmin(ResourceScopedAdminMixin, admin.ModelAdmin):
    """Versions are history: append-only, and only DocumentService writes them."""

    resource_id_fields = ("document__resource_id",)
    list_display = ("document", "version", "change_type", "source", "created_by", "created_at")
    list_filter = ("change_type", "source")
    search_fields = ("document__title",)
    readonly_fields = [field.name for field in DocumentVersion._meta.fields]
