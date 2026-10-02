"""Read-only admin for the secret vault.

A secret is not a ``Resource``: it is user-owned, and
:meth:`SecretService.can_use` resolves it by owner id with no superuser bypass
and no group path. The list is therefore cut down to the caller's own secrets,
which is the same answer the service gives - an operator cannot read somebody
else's vault from ``/admin/``.

Write path: :class:`~apps.secrets.services.SecretService` (create, update,
attach, detach, reveal). ``reveal`` is the only way the plaintext ever leaves the
vault, and it writes an audit event; the admin shows ciphertext, never a value.
"""

from django.contrib import admin
from django.db.models import Q

from apps.permissions.constants import Permission
from apps.permissions.services import PermissionService
from apps.resources.admin import ReadOnlyAdminMixin, ReadOnlyInlineMixin

from .models import Secret, SecretAttachment


class SecretAttachmentInline(ReadOnlyInlineMixin, admin.TabularInline):
    """Safe to show inline: the parent secret is already filtered to its owner.

    Read-only because ``SecretService.attach`` / ``detach`` are the write path -
    they exist to record *where* a secret may be used, and a stray row here would
    silently widen that.
    """

    model = SecretAttachment
    fields = ("workspace", "project", "created_at")
    readonly_fields = fields


@admin.register(Secret)
class SecretAdmin(ReadOnlyAdminMixin, admin.ModelAdmin):
    list_display = ("name", "owner", "secret_type", "is_active", "last_used_at", "created_at")
    list_filter = ("secret_type", "is_active")
    search_fields = ("name", "owner__username")
    readonly_fields = (
        "owner",
        "encrypted_payload",
        "fingerprint",
        "last_used_at",
        "created_at",
        "updated_at",
    )
    inlines = [SecretAttachmentInline]

    def get_queryset(self, request):
        # Mirrors SecretService.can_use: the owner, and nobody else.
        return super().get_queryset(request).filter(owner=request.user)


@admin.register(SecretAttachment)
class SecretAttachmentAdmin(ReadOnlyAdminMixin, admin.ModelAdmin):
    """A secret granted for use in a workspace/project.

    Visible when the caller owns the secret or may read the workspace/project it
    is attached to. The workspace and project primary keys *are* resource ids, so
    the scope check is the same
    :meth:`PermissionService.allowed_resource_ids` call the rest of the admin
    uses.
    """

    list_display = ("secret", "workspace", "project", "created_at")
    readonly_fields = [field.name for field in SecretAttachment._meta.fields]

    def get_queryset(self, request):
        queryset = super().get_queryset(request)
        scope_ids = set(queryset.values_list("workspace_id", flat=True))
        scope_ids.update(queryset.values_list("project_id", flat=True))
        scope_ids.discard(None)
        readable = set(
            PermissionService.allowed_resource_ids(request.user, scope_ids, Permission.READ)
        )
        return queryset.filter(
            Q(secret__owner=request.user)
            | Q(workspace_id__in=readable)
            | Q(project_id__in=readable)
        )
