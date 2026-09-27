from django.contrib import admin
from django.urls import reverse
from django.utils.html import format_html

from .models import Project, Workspace


class PermissionsLinkMixin:
    def permission_link(self, obj):
        url = reverse("web:resource_permissions", args=[obj.resource_id])
        return format_html('<a href="{}" target="_blank">Manage permissions →</a>', url)

    permission_link.short_description = "Access control"


@admin.register(Workspace)
class WorkspaceAdmin(PermissionsLinkMixin, admin.ModelAdmin):
    list_display = ("name", "slug", "created_at")
    search_fields = ("name", "slug")
    prepopulated_fields = {"slug": ("name",)}
    readonly_fields = ("resource", "permission_link", "created_at", "updated_at")


@admin.register(Project)
class ProjectAdmin(PermissionsLinkMixin, admin.ModelAdmin):
    list_display = ("name", "workspace", "slug", "created_at")
    list_filter = ("workspace",)
    search_fields = ("name", "slug")
    readonly_fields = ("resource", "permission_link", "created_at", "updated_at")
