"""Read-only admin for workspaces and projects.

Write path: :class:`~apps.workspaces.services.WorkspaceService` /
:class:`~apps.workspaces.services.ProjectService` (creation, which also issues
the owner's ADMIN grant) and
:class:`~apps.workspaces.ownership.OwnershipService` (transfer, kind, takeover).
"""

from django.contrib import admin

from apps.resources.admin import ResourceBackedAdminMixin

from .models import Project, Workspace


@admin.register(Workspace)
class WorkspaceAdmin(ResourceBackedAdminMixin, admin.ModelAdmin):
    # `owner` and `kind` decide who is inside and who can hand out access, and
    # they are kept in step with the ACL by OwnershipService, never by hand.
    list_display = ("name", "slug", "kind", "owner", "created_at")
    list_filter = ("kind",)
    search_fields = ("name", "slug")
    readonly_fields = (
        "resource",
        "permission_link",
        "owner",
        "kind",
        "created_by",
        "created_at",
        "updated_at",
    )


@admin.register(Project)
class ProjectAdmin(ResourceBackedAdminMixin, admin.ModelAdmin):
    list_display = ("name", "workspace", "slug", "owner", "created_at")
    list_filter = ("workspace",)
    search_fields = ("name", "slug")
    # A project has no `kind`: it is shared or it is personal purely because of
    # the workspace it lives in.
    readonly_fields = (
        "resource",
        "permission_link",
        "owner",
        "created_by",
        "created_at",
        "updated_at",
    )
