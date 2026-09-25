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

from .models import Project, Workspace


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


class WorkspaceService:
    @staticmethod
    @transaction.atomic
    def create(*, name: str, slug: str | None = None, description: str = "", created_by=None,
               request=None) -> Workspace:
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
            created_by=created_by,
        )
        if created_by is not None:
            PermissionService.grant(
                resource,
                subject_type=SubjectType.USER,
                subject_id=created_by.id,
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
            detail={"type": "workspace", "name": name},
        )
        return workspace


class ProjectService:
    @staticmethod
    @transaction.atomic
    def create(*, workspace: Workspace, name: str, slug: str | None = None,
               description: str = "", created_by=None, request=None) -> Project:
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
        )
        if created_by is not None:
            PermissionService.grant(
                resource,
                subject_type=SubjectType.USER,
                subject_id=created_by.id,
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
