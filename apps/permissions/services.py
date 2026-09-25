"""Central permission engine.

Every REST/MCP/web entry point resolves access through this service. ACLs live
on Resources; a domain object's resource is reached via ``obj.resource``.

Resolution order (most specific first):
    resource -> parent resource -> ... -> workspace
At the first level that yields a decision, DENY wins over ALLOW. A DENY on a
lower permission cascades to higher ones (denying `read` blocks `write`). If
nothing matches, access is denied. An API key can only narrow, never widen,
its owner's permissions.
"""

from __future__ import annotations

from django.db.models import Q

from apps.groups.models import GroupMembership
from apps.resources.models import Resource

from .constants import Effect, Permission, SubjectType, allows, deny_blocks
from .models import ResourceACL


class PermissionService:
    # -- public API ----------------------------------------------------------
    @classmethod
    def check(cls, user, resource, permission: str, api_key=None) -> bool:
        if user is None or not getattr(user, "is_authenticated", False):
            return False
        if resource is None:
            return False
        if not cls._user_check(user, resource, permission):
            return False
        if api_key is not None and not cls._api_key_check(api_key, resource, permission):
            return False
        return True

    @classmethod
    def allowed_resource_ids(cls, user, resource_ids, permission: str, api_key=None) -> list:
        ids = list(resource_ids)
        if not ids:
            return []
        if user is None or not getattr(user, "is_authenticated", False):
            return []
        if getattr(user, "is_superuser", False) and api_key is None:
            return ids
        resources = Resource.objects.filter(id__in=ids).select_related("parent")
        return [r.id for r in resources if cls.check(user, r, permission, api_key)]

    # -- user / group resolution --------------------------------------------
    @classmethod
    def _user_check(cls, user, resource: Resource, permission: str) -> bool:
        if getattr(user, "is_superuser", False):
            return True
        group_ids = cls._group_ids(user)
        for index, node in enumerate(resource.ancestors()):
            entries = cls._entries_for(node, user, group_ids)
            if index > 0:
                entries = [entry for entry in entries if entry.inherit]
            if not entries:
                continue
            if any(
                entry.effect == Effect.DENY and deny_blocks(entry.permission, permission)
                for entry in entries
            ):
                return False
            if any(
                entry.effect == Effect.ALLOW and allows(entry.permission, permission)
                for entry in entries
            ):
                return True
        return False

    @classmethod
    def _entries_for(cls, resource: Resource, user, group_ids) -> list[ResourceACL]:
        query = Q(subject_type=SubjectType.USER, subject_id=user.id)
        if group_ids:
            query |= Q(subject_type=SubjectType.GROUP, subject_id__in=group_ids)
        return list(ResourceACL.objects.filter(resource=resource).filter(query))

    @classmethod
    def _group_ids(cls, user) -> list:
        return list(
            GroupMembership.objects.filter(user=user).values_list("group_id", flat=True)
        )

    # -- api key narrowing ---------------------------------------------------
    @classmethod
    def _api_key_check(cls, api_key, resource: Resource, permission: str) -> bool:
        scopes = list(api_key.scopes.all())
        if not scopes:
            return True
        workspace = resource.workspace_resource()
        project = resource.project_resource()
        workspace_id = workspace.id if workspace else None
        project_id = project.id if project else None
        applicable = [
            scope for scope in scopes if scope.applies_to(workspace_id, project_id)
        ]
        if not applicable:
            return False
        if any(
            scope.effect == Effect.DENY and deny_blocks(scope.permission, permission)
            for scope in applicable
        ):
            return False
        if any(
            scope.effect == Effect.ALLOW and allows(scope.permission, permission)
            for scope in applicable
        ):
            return True
        return False

    # -- mutations -----------------------------------------------------------
    @classmethod
    def grant(
        cls,
        resource: Resource,
        *,
        subject_type: str,
        subject_id,
        permission: str,
        effect: str = Effect.ALLOW,
        inherit: bool = True,
        created_by=None,
    ) -> ResourceACL:
        entry, _ = ResourceACL.objects.update_or_create(
            resource=resource,
            subject_type=subject_type,
            subject_id=subject_id,
            permission=permission,
            defaults={"effect": effect, "inherit": inherit, "created_by": created_by},
        )
        return entry

    @classmethod
    def revoke(cls, resource: Resource, *, subject_type: str, subject_id, permission=None):
        queryset = ResourceACL.objects.filter(
            resource=resource, subject_type=subject_type, subject_id=subject_id
        )
        if permission:
            queryset = queryset.filter(permission=permission)
        return queryset.delete()


__all__ = ["PermissionService", "Permission"]
