"""DRF permission plumbing on top of the central PermissionService."""

from __future__ import annotations

from rest_framework.permissions import BasePermission

from apps.accounts.models import ApiKey
from apps.permissions.constants import Permission
from apps.permissions.services import PermissionService
from apps.resources.models import Resource

METHOD_PERMISSION = {
    "GET": Permission.READ,
    "HEAD": Permission.READ,
    "OPTIONS": Permission.READ,
    "POST": Permission.WRITE,
    "PUT": Permission.WRITE,
    "PATCH": Permission.WRITE,
    "DELETE": Permission.DELETE,
}


def api_key_from_request(request):
    auth = getattr(request, "auth", None)
    return auth if isinstance(auth, ApiKey) else None


def resource_of(obj):
    if isinstance(obj, Resource):
        return obj
    return getattr(obj, "resource", None)


class ResourcePermission(BasePermission):
    """Object level check using the required permission of the current action."""

    def has_permission(self, request, view):
        return bool(request.user and request.user.is_authenticated)

    def has_object_permission(self, request, view, obj):
        resource = resource_of(obj)
        if resource is None:
            return True
        required = getattr(view, "required_permission", None) or METHOD_PERMISSION.get(
            request.method, Permission.READ
        )
        return PermissionService.check(
            request.user, resource, required, api_key=api_key_from_request(request)
        )


class PermissionFilterMixin:
    """Filters list querysets down to resources the caller may read."""

    resource_field = "resource_id"

    def required_for_action(self, action: str) -> str:
        if action in {"create", "update", "partial_update"}:
            return Permission.WRITE
        if action == "destroy":
            return Permission.DELETE
        return Permission.READ

    def filter_queryset(self, queryset):
        queryset = super().filter_queryset(queryset)
        action = getattr(self, "action", None)
        required = self.required_for_action(action) if action else Permission.READ
        resource_ids = list(queryset.values_list(self.resource_field, flat=True))
        allowed = PermissionService.allowed_resource_ids(
            self.request.user,
            resource_ids,
            required,
            api_key=api_key_from_request(self.request),
        )
        return queryset.filter(**{f"{self.resource_field}__in": allowed})
