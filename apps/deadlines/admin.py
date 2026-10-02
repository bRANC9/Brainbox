"""Read-only admin for extracted deadlines.

Write path: :class:`~apps.deadlines.services.DeadlineService`, which re-extracts
from the document and preserves the manually added rows. ``confidence`` and
``source_ref`` are extractor output, so editing them here would put the
admin's guess in the same row as the model's.
"""

from django.contrib import admin

from apps.resources.admin import ResourceScopedAdminMixin

from .models import KnowledgeDeadline


@admin.register(KnowledgeDeadline)
class KnowledgeDeadlineAdmin(ResourceScopedAdminMixin, admin.ModelAdmin):
    resource_id_fields = ("resource_id",)
    list_display = ("due_date", "title", "document", "workspace", "project", "status", "source", "confidence")
    list_filter = ("status", "source", "workspace", "project")
    search_fields = ("title", "document__title", "context")
    date_hierarchy = "due_date"
    readonly_fields = (
        "document",
        "resource",
        "workspace",
        "project",
        "source",
        "confidence",
        "source_ref",
        "created_at",
        "updated_at",
    )
