"""Ownership: transfer, takeover, and the Personal/shared switch.

Ownership is a single user FK on Workspace/Project, kept in sync with the ACL by
this module - the owner's ADMIN entry is the *effect* of the field, never an
independent thing. That is why every mutation goes through here rather than
through a plain ``.save()``.

Three operations, three very different trust levels:

* :meth:`OwnershipService.transfer` - the owner (or a superuser that has taken
  over) hands the object to somebody else. Ownership is not a privilege that can
  be escalated, so it is deliberately *not* a shareable grant.
* :meth:`OwnershipService.take_over` - the explicit, audited way for a superuser
  to get a foot in the door. It writes a normal ACL entry and does **not** touch
  the owner, so it cannot become a way to lock the owner out. Blocked entirely by
  ``Resource.no_takeover`` on the resource or any ancestor.
* :meth:`OwnershipService.set_kind` - Personal <-> shared. A Personal workspace
  has exactly one holder and cannot be transferred; converting it to shared is
  the deliberate way out.
"""

from __future__ import annotations

from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction

from apps.audit.models import AuditAction, AuditSource
from apps.audit.services import AuditService
from apps.permissions.constants import Effect, Permission, SubjectType
from apps.permissions.services import PermissionService
from apps.resources.models import ResourceType

from .models import Project, Workspace, WorkspaceKind


def _model_for(resource):
    """The Workspace or Project a resource stands for, or None."""
    if resource.resource_type == ResourceType.WORKSPACE:
        return getattr(resource, "workspace", None)
    if resource.resource_type == ResourceType.PROJECT:
        return getattr(resource, "project", None)
    return None


class OwnershipService:
    # -- who may act ---------------------------------------------------------
    @staticmethod
    def _is_object_owner(actor, resource) -> bool:
        obj = _model_for(resource)
        if obj is None:
            return False
        owner_id = getattr(obj, "owner_id", None)
        return bool(owner_id and str(owner_id) == str(actor.id))

    @classmethod
    def can_transfer(cls, actor, resource) -> bool:
        if resource is None or actor is None:
            return False
        if not getattr(actor, "is_authenticated", False):
            return False
        if cls._is_object_owner(actor, resource):
            return True
        # Break-glass: a superuser that already took over may still fix a
        # mis-assigned owner, but only on a resource it holds admin on directly.
        return bool(actor.is_superuser and PermissionService._has_direct_admin_entry(
            actor, resource
        ))

    # -- transfer ------------------------------------------------------------
    @classmethod
    @transaction.atomic
    def transfer(cls, *, resource, new_owner, actor, keep_old_access: str | None = None,
                 request=None):
        """Hand ``resource`` to ``new_owner``.

        ``keep_old_access`` is ``None`` (the previous owner loses their grant on
        this resource), or ``Permission.READ`` / ``Permission.WRITE`` to keep a
        weaker hold. It can never be ADMIN: that is what is being given away.
        """
        if not cls.can_transfer(actor, resource):
            raise PermissionDenied("You cannot transfer ownership of this resource.")
        obj = _model_for(resource)
        if obj is None:
            raise ValidationError("Only a workspace or a project can be transferred.")
        if getattr(obj, "kind", None) == WorkspaceKind.PERSONAL:
            raise ValidationError(
                "A Personal workspace cannot be transferred. Convert it to a shared "
                "workspace first if you really want to hand it over."
            )
        if new_owner is None or not new_owner.is_active:
            raise ValidationError("The new owner must be an active user.")
        if new_owner.id == obj.owner_id:
            return obj

        previous_owner = obj.owner
        PermissionService.grant(
            resource,
            subject_type=SubjectType.USER,
            subject_id=new_owner.id,
            permission=Permission.ADMIN,
            created_by=actor,
        )
        if previous_owner is not None and previous_owner.id != new_owner.id:
            if keep_old_access in (Permission.READ, Permission.WRITE):
                # Downgrade FIRST, then drop the ADMIN row. The other order
                # would leave "keep read access" with an admin, and - worse -
                # revoking the actor's own ADMIN would make this very grant
                # impossible, since the actor is the previous owner.
                PermissionService.grant(
                    resource,
                    subject_type=SubjectType.USER,
                    subject_id=previous_owner.id,
                    permission=keep_old_access,
                    effect=Effect.ALLOW,
                    inherit=True,
                    created_by=actor,
                )
            # Only this resource's ADMIN entry is removed. Anything they hold
            # elsewhere (a group, a nested project) is untouched, so handing a
            # workspace over does not silently evict them from a subtree they
            # were granted separately.
            PermissionService.revoke(
                resource,
                subject_type=SubjectType.USER,
                subject_id=previous_owner.id,
                permission=Permission.ADMIN,
                actor=actor,
            )
        obj.owner = new_owner
        obj.save(update_fields=["owner", "updated_at"])
        AuditService.log(
            AuditAction.CHANGE_PERMISSION,
            user=actor,
            resource=resource,
            workspace=obj if isinstance(obj, Workspace) else obj.workspace,
            project=obj if isinstance(obj, Project) else None,
            source=AuditSource.WEB if request is not None else AuditSource.SYSTEM,
            request=request,
            detail={
                "type": "ownership_transfer",
                "from": str(previous_owner.id) if previous_owner else None,
                "to": str(new_owner.id),
                "kept_previous_access": keep_old_access,
            },
        )
        return obj

    # -- takeover ------------------------------------------------------------
    @classmethod
    @transaction.atomic
    def take_over(cls, *, resource, actor, request=None):
        if not PermissionService.can_take_over(actor, resource):
            raise PermissionDenied(
                "This resource cannot be taken over, or you are not allowed to."
            )
        entry = PermissionService.grant_unchecked(
            resource,
            subject_type=SubjectType.USER,
            subject_id=actor.id,
            permission=Permission.ADMIN,
            inherit=True,
            created_by=actor,
        )
        AuditService.log(
            AuditAction.CHANGE_PERMISSION,
            user=actor,
            resource=resource,
            source=AuditSource.WEB if request is not None else AuditSource.SYSTEM,
            request=request,
            detail={"type": "takeover", "permission": Permission.ADMIN},
        )
        return entry

    # -- Personal <-> shared -------------------------------------------------
    @classmethod
    @transaction.atomic
    def set_kind(cls, *, workspace, kind: str, actor, request=None):
        if not PermissionService.is_scope_owner(actor, workspace.resource):
            raise PermissionDenied("Only the owner can change the workspace kind.")
        if kind not in WorkspaceKind.values:
            raise ValidationError(f"Unknown workspace kind: {kind}")
        if workspace.kind == kind:
            return workspace
        if kind == WorkspaceKind.PERSONAL:
            users, groups = PermissionService.grantable_subjects(actor, workspace.resource)
            if len(users | groups) > 1:
                raise ValidationError(
                    "Ez a workspace még megosztott, nem alakítható személyesé. "
                    "Vond be a jogokat, vagy hozz létre új személyes workspace-t."
                )
        previous = workspace.kind
        workspace.kind = kind
        workspace.save(update_fields=["kind", "updated_at"])
        AuditService.log(
            AuditAction.CHANGE_PERMISSION,
            user=actor,
            resource=workspace.resource,
            workspace=workspace,
            source=AuditSource.WEB if request is not None else AuditSource.SYSTEM,
            request=request,
            detail={"type": "workspace_kind", "from": previous, "to": kind},
        )
        return workspace


__all__ = ["OwnershipService"]
