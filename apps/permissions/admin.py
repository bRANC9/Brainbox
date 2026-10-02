"""Read-only admin for the ACL table itself.

Write path: :meth:`PermissionService.grant` /
:meth:`~apps.permissions.services.PermissionService.revoke` and, for a
superuser, :meth:`OwnershipService.take_over` - all three validate the caller and
all three write an audit event. A hand-inserted ``ResourceACL`` row would do
none of that, which is precisely how an unauthorised entry gets into the chain.
"""

from django.contrib import admin

from apps.resources.admin import ResourceScopedAdminMixin

from .models import ResourceACL


@admin.register(ResourceACL)
class ResourceACLAdmin(ResourceScopedAdminMixin, admin.ModelAdmin):
    list_display = (
        "resource",
        "subject_type",
        "subject_id",
        "permission",
        "effect",
        "inherit",
        "created_by",
        "created_at",
    )
    list_filter = ("subject_type", "permission", "effect", "inherit")
    search_fields = ("resource__name", "subject_id")
    readonly_fields = (
        "resource",
        "subject_type",
        "subject_id",
        "permission",
        "effect",
        "inherit",
        "created_by",
        "created_at",
    )
