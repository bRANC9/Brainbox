"""DRF permission plumbing on top of the central PermissionService."""

from __future__ import annotations

from rest_framework.permissions import BasePermission

from apps.accounts.models import ApiKey
from apps.groups.models import GroupMembership
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
            # Deny by default. An object this class cannot resolve a Resource
            # for is an object it has no rule for, and "no rule" must never
            # mean "allowed": a permissive fallback turns every viewset that
            # forgets to attach a Resource into an open door. A viewset with a
            # genuinely different question to ask declares its own permission
            # class instead (see ResourceACLPermission below).
            return False
        required = getattr(view, "required_permission", None) or METHOD_PERMISSION.get(
            request.method, Permission.READ
        )
        return PermissionService.check(
            request.user, resource, required, api_key=api_key_from_request(request)
        )


class RelatedResourcePermission(BasePermission):
    """Object check against a *named* relation, for rows with no ``resource``.

    A ``ResourceLink`` names its two ends ``source``/``target``, so
    :class:`ResourcePermission` has nothing to resolve for it and (correctly)
    denies. The viewset says which side is the one being modified via
    ``resource_field`` -- for a link that is the source, the side a write acts
    on -- and the same required-permission rule as everywhere else applies.
    """

    def has_permission(self, request, view):
        return bool(request.user and request.user.is_authenticated)

    def has_object_permission(self, request, view, obj):
        field = getattr(view, "resource_field", None) or "resource_id"
        resource = Resource.objects.filter(pk=getattr(obj, field, None)).first()
        if resource is None:
            return False
        required = getattr(view, "required_permission", None) or METHOD_PERMISSION.get(
            request.method, Permission.READ
        )
        return PermissionService.check(
            request.user, resource, required, api_key=api_key_from_request(request)
        )


class ResourceACLPermission(BasePermission):
    """Object check for ``ResourceACL`` rows.

    An ACL row is a *permission* record, not content, so the question is not
    "may this caller read this resource" but "may this caller manage this
    resource's ACL". It also resolves its target from ``resource_id`` rather
    than relying on ``resource_of``: an unsaved row has no ``resource``
    attribute at all, which is exactly the case
    :class:`ResourcePermission` now denies.
    """

    message = "Ezt a hozzáférés-kezelést nem módosíthatod."

    def has_permission(self, request, view):
        return bool(request.user and request.user.is_authenticated)

    def has_object_permission(self, request, view, obj):
        resource = Resource.objects.filter(pk=obj.resource_id).first()
        if resource is None:
            return False
        return PermissionService.can_manage_acl(
            request.user, resource, api_key=api_key_from_request(request)
        )


class SuperuserOnly(BasePermission):
    """Superuser-only.

    Not ``IsAdminUser``: ``is_staff`` is not a trust level in Brainbox. Group
    administration in particular must stay out of its reach, because adding
    yourself to a group that already holds access somewhere *is* the
    escalation.
    """

    message = "Ez a művelet csak superusereknek érhető el."

    def has_permission(self, request, view):
        user = request.user
        return bool(user and user.is_authenticated and user.is_superuser)


def can_administer_group(user, group) -> bool:
    """A superuser, or one of the group's managers. Never ``is_staff`` alone."""
    if group is None or user is None or not getattr(user, "is_authenticated", False):
        return False
    if user.is_superuser:
        return True
    return GroupMembership.objects.filter(
        group=group, user=user, role=GroupMembership.Role.MANAGER
    ).exists()


class GroupAdminPermission(BasePermission):
    """Group administration: the group's managers, or a superuser.

    ``GroupMembership.Role.MANAGER`` administers the group (add/remove members).
    Holding it grants no access to any resource -- it only says who may change
    the membership -- so it is checked here rather than through the permission
    engine. ``is_staff`` gets nothing: a staff user could otherwise add
    themselves to a group that is granted access somewhere and be handed that
    access by the next request.
    """

    message = "A csoportot csak a vezetői vagy egy superuser kezelheti."

    def has_permission(self, request, view):
        return bool(request.user and request.user.is_authenticated)

    def has_object_permission(self, request, view, obj):
        return can_administer_group(request.user, obj)


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
