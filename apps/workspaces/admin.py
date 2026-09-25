from django.contrib import admin

from .models import Project, Workspace


@admin.register(Workspace)
class WorkspaceAdmin(admin.ModelAdmin):
    list_display = ("name", "slug", "created_at")
    search_fields = ("name", "slug")
    prepopulated_fields = {"slug": ("name",)}


@admin.register(Project)
class ProjectAdmin(admin.ModelAdmin):
    list_display = ("name", "workspace", "slug", "created_at")
    list_filter = ("workspace",)
    search_fields = ("name", "slug")
