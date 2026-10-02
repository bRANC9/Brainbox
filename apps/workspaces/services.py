"""Application services for workspaces and projects."""

from __future__ import annotations

from django.db import transaction
from django.utils.text import slugify

from apps.audit.models import AuditAction, AuditSource
from apps.audit.services import AuditService
from apps.permissions.constants import Permission, SubjectType
from apps.permissions.services import PermissionService
from apps.resources.models import ResourceType
from apps.resources.services import ResourceService

from .models import Project, Workspace, WorkspaceKind


def _unique_workspace_slug(base: str) -> str:
    base = base or "workspace"
    slug = base
    counter = 1
    while Workspace.objects.filter(slug=slug).exists():
        counter += 1
        slug = f"{base}-{counter}"
    return slug


def _unique_project_slug(workspace: Workspace, base: str) -> str:
    base = base or "project"
    slug = base
    counter = 1
    while Project.objects.filter(workspace=workspace, slug=slug).exists():
        counter += 1
        slug = f"{base}-{counter}"
    return slug


def _require_actor(created_by, *, what: str):
    """Refuse to create an object nobody owns.

    Ownership in Brainbox is not a field: it is the implicit
    ``Permission.ADMIN`` grant issued to ``created_by`` below. With no actor
    the row would be written with ``created_by=NULL`` and *no ACL entry at
    all*, so :class:`PermissionService` denies it to everyone - a ghost object
    that the creator cannot see either. Refusing is the only safe outcome.
    """
    if created_by is None:
        raise ValueError(f"{what} requires created_by; ownerless objects are refused.")
    if not getattr(created_by, "is_authenticated", True):
        raise ValueError(f"{what} requires an authenticated created_by.")
    return created_by


class WorkspaceService:
    @staticmethod
    @transaction.atomic
    def create(*, name: str, slug: str | None = None, description: str = "",
               created_by, kind: str = WorkspaceKind.SHARED, owner=None,
               request=None) -> Workspace:
        _require_actor(created_by, what="Workspace creation")
        owner = owner or created_by
        base_slug = slugify(slug or name)
        resource = ResourceService.create(
            resource_type=ResourceType.WORKSPACE,
            name=name,
            created_by=created_by,
        )
        workspace = Workspace.objects.create(
            resource=resource,
            name=name,
            slug=_unique_workspace_slug(base_slug),
            description=description,
            kind=kind,
            created_by=created_by,
            owner=owner,
        )
        # The owner's implicit ADMIN grant. Unchecked on purpose: the creator
        # has no grant yet, and at this point *is* the owner by construction.
        PermissionService.grant_unchecked(
            resource,
            subject_type=SubjectType.USER,
            subject_id=owner.id,
            permission=Permission.ADMIN,
            created_by=created_by,
        )
        AuditService.log(
            AuditAction.CREATE,
            user=created_by,
            resource=resource,
            workspace=workspace,
            source=AuditSource.API if request is not None else AuditSource.SYSTEM,
            request=request,
            detail={"type": "workspace", "name": name, "kind": kind},
        )
        return workspace


class ProjectService:
    @staticmethod
    @transaction.atomic
    def create(*, workspace: Workspace, name: str, slug: str | None = None,
               description: str = "", created_by, owner=None, request=None) -> Project:
        _require_actor(created_by, what="Project creation")
        owner = owner or created_by
        base_slug = slugify(slug or name)
        resource = ResourceService.create(
            resource_type=ResourceType.PROJECT,
            name=name,
            created_by=created_by,
            parent=workspace.resource,
        )
        project = Project.objects.create(
            resource=resource,
            workspace=workspace,
            name=name,
            slug=_unique_project_slug(workspace, base_slug),
            description=description,
            created_by=created_by,
            owner=owner,
        )
        PermissionService.grant_unchecked(
            resource,
            subject_type=SubjectType.USER,
            subject_id=owner.id,
            permission=Permission.ADMIN,
            created_by=created_by,
        )
        AuditService.log(
            AuditAction.CREATE,
            user=created_by,
            resource=resource,
            workspace=workspace,
            project=project,
            source=AuditSource.API if request is not None else AuditSource.SYSTEM,
            request=request,
            detail={"type": "project", "name": name},
        )
        return project
