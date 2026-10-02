"""Read-only admin for the resource link graph.

Write path: :class:`~apps.links.services.LinkService`, which is called by
:class:`~apps.documents.services.DocumentService` when content changes. A link
whose source is gone (or whose target the caller may not read) is a broken edge
in the graph, so the rows are never edited by hand.
"""

from django.contrib import admin

from apps.resources.admin import ResourceScopedAdminMixin

from .models import ResourceLink


@admin.register(ResourceLink)
class ResourceLinkAdmin(ResourceScopedAdminMixin, admin.ModelAdmin):
    # Listed when *either* end is readable: a link out of something you can see
    # is part of what you can see. Nothing about the other end is exposed.
    resource_id_fields = ("source_id", "target_id")
    list_display = ("source", "link_type", "target", "created_at")
    list_filter = ("link_type",)
    search_fields = ("source__name", "target__name")
    readonly_fields = [field.name for field in ResourceLink._meta.fields]
