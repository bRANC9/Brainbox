from django.contrib import admin

from apps.resources.admin import ResourceBackedAdminMixin

from .models import Project, Workspace


@admin.register(Workspace)
class WorkspaceAdmin(ResourceBackedAdminMixin, admin.ModelAdmin):
    list_display = ("name", "slug", "created_at")
    search_fields = ("name", "slug")
    prepopulated_fields = {"slug": ("name",)}
    readonly_fields = ("resource", "permission_link", "created_at", "updated_at")


@admin.register(Project)
class ProjectAdmin(ResourceBackedAdminMixin, admin.ModelAdmin):
    list_display = ("name", "workspace", "slug", "created_at")
    list_filter = ("workspace",)
    search_fields = ("name", "slug")
    readonly_fields = ("resource", "permission_link", "created_at", "updated_at")