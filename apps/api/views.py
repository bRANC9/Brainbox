"""REST API views. All object access is funneled through the permission engine."""

from __future__ import annotations

import difflib

from django.contrib.auth import get_user_model
from django.http import HttpResponse
from django.shortcuts import get_object_or_404
from rest_framework import status, viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import PermissionDenied, ValidationError
from rest_framework.parsers import FormParser, MultiPartParser
from rest_framework.permissions import IsAdminUser, IsAuthenticated
from rest_framework.response import Response

from apps.accounts.models import ApiKey
from apps.accounts.services import ApiKeyService
from apps.audit.models import AuditEvent
from apps.documents.models import Document, DocumentVersion
from apps.documents.services import DocumentService
from apps.files.models import File
from apps.files.services import FileService
from apps.git.git_cli import GitError
from apps.git.models import GitRepository
from apps.git.services import GitService
from apps.groups.models import Group
from apps.links.models import ResourceLink
from apps.links.services import LinkService
from apps.permissions.constants import Permission
from apps.permissions.models import ResourceACL
from apps.permissions.services import PermissionService
from apps.resources.models import Resource
from apps.workspaces.models import Project, Workspace

from . import serializers as s
from .permissions import (
    PermissionFilterMixin,
    ResourcePermission,
    api_key_from_request,
)

User = get_user_model()


class CreatePermissionMixin:
    """Checks write/admin permission against the create target resource."""

    create_permission = Permission.WRITE

    def get_create_target(self, validated_data):
        return None

    def create(self, request, *args, **kwargs):
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        target = self.get_create_target(serializer.validated_data)
        if target is not None and not PermissionService.check(
            request.user, target, self.create_permission, api_key=api_key_from_request(request)
        ):
            raise PermissionDenied("You do not have permission to create this resource.")
        self.perform_create(serializer)
        headers = self.get_success_headers(serializer.data)
        return Response(serializer.data, status=status.HTTP_201_CREATED, headers=headers)


# ---------------------------------------------------------------------------
# Workspaces / projects
# ---------------------------------------------------------------------------
class WorkspaceViewSet(CreatePermissionMixin, PermissionFilterMixin, viewsets.ModelViewSet):
    queryset = Workspace.objects.select_related("resource")
    serializer_class = s.WorkspaceSerializer
    permission_classes = [IsAuthenticated, ResourcePermission]
    search_fields = ["name", "slug"]
    ordering_fields = ["name", "created_at"]


class ProjectViewSet(CreatePermissionMixin, PermissionFilterMixin, viewsets.ModelViewSet):
    queryset = Project.objects.select_related("resource", "workspace")
    serializer_class = s.ProjectSerializer
    permission_classes = [IsAuthenticated, ResourcePermission]
    search_fields = ["name", "slug"]
    ordering_fields = ["name", "created_at"]

    def get_queryset(self):
        queryset = super().get_queryset()
        workspace_id = self.request.query_params.get("workspace")
        if workspace_id:
            queryset = queryset.filter(workspace_id=workspace_id)
        return queryset

    def get_create_target(self, validated_data):
        workspace = validated_data.get("workspace")
        return workspace.resource if workspace else None


# ---------------------------------------------------------------------------
# Resources / ACL / links
# ---------------------------------------------------------------------------
class ResourceViewSet(PermissionFilterMixin, viewsets.ReadOnlyModelViewSet):
    queryset = Resource.objects.select_related("parent")
    serializer_class = s.ResourceSerializer
    permission_classes = [IsAuthenticated, ResourcePermission]
    resource_field = "id"
    search_fields = ["name"]
    ordering_fields = ["name", "created_at"]

    def get_queryset(self):
        queryset = super().get_queryset()
        resource_type = self.request.query_params.get("type")
        if resource_type:
            queryset = queryset.filter(resource_type=resource_type)
        return queryset


class ResourceACLViewSet(CreatePermissionMixin, PermissionFilterMixin, viewsets.ModelViewSet):
    queryset = ResourceACL.objects.select_related("resource")
    serializer_class = s.ResourceACLSerializer
    permission_classes = [IsAuthenticated, ResourcePermission]
    create_permission = Permission.ADMIN
    required_permission = Permission.ADMIN

    def get_queryset(self):
        queryset = super().get_queryset()
        resource_id = self.request.query_params.get("resource")
        if resource_id:
            queryset = queryset.filter(resource_id=resource_id)
        return queryset

    def required_for_action(self, action):
        return Permission.ADMIN

    def get_create_target(self, validated_data):
        return validated_data.get("resource")


class ResourceLinkViewSet(CreatePermissionMixin, PermissionFilterMixin, viewsets.ModelViewSet):
    queryset = ResourceLink.objects.select_related("source", "target")
    serializer_class = s.ResourceLinkSerializer
    permission_classes = [IsAuthenticated]
    resource_field = "source_id"

    def get_queryset(self):
        queryset = super().get_queryset()
        resource_id = self.request.query_params.get("resource")
        direction = self.request.query_params.get("direction", "outgoing")
        if resource_id:
            if direction == "incoming":
                queryset = queryset.filter(target_id=resource_id)
            else:
                queryset = queryset.filter(source_id=resource_id)
        return queryset

    def required_for_action(self, action):
        return Permission.READ

    def get_create_target(self, validated_data):
        return validated_data.get("source")

    def perform_destroy(self, instance):
        LinkService.delete(source=instance.source, target=instance.target, link_type=instance.link_type)


# ---------------------------------------------------------------------------
# Documents
# ---------------------------------------------------------------------------
class DocumentViewSet(CreatePermissionMixin, PermissionFilterMixin, viewsets.ModelViewSet):
    queryset = Document.objects.select_related("resource", "workspace", "project")
    serializer_class = s.DocumentSerializer
    permission_classes = [IsAuthenticated, ResourcePermission]
    search_fields = ["title", "summary", "path"]
    ordering_fields = ["updated_at", "created_at", "title", "priority"]

    def get_queryset(self):
        queryset = super().get_queryset()
        params = self.request.query_params
        if params.get("workspace"):
            queryset = queryset.filter(workspace_id=params["workspace"])
        if params.get("project"):
            queryset = queryset.filter(project_id=params["project"])
        if params.get("status"):
            queryset = queryset.filter(status=params["status"])
        return queryset

    def get_serializer_context(self):
        context = super().get_serializer_context()
        context["include_content"] = self.action != "list"
        return context

    def get_create_target(self, validated_data):
        project = validated_data.get("project")
        if project is not None:
            return project.resource
        workspace = validated_data.get("workspace")
        return workspace.resource if workspace else None

    def perform_destroy(self, instance):
        DocumentService.delete(
            document=instance,
            user=self.request.user,
            request=self.request,
            api_key=api_key_from_request(self.request),
        )

    @action(detail=True, methods=["get"])
    def versions(self, request, pk=None):
        document = self.get_object()
        data = s.DocumentVersionSerializer(document.versions.all(), many=True).data
        return Response(data)

    @action(detail=True, methods=["post"], url_path="restore")
    def restore(self, request, pk=None):
        document = self.get_object()
        version_number = request.data.get("version")
        if version_number is None:
            raise ValidationError({"version": "This field is required."})
        DocumentService.restore(
            document=document,
            version_number=int(version_number),
            user=request.user,
            request=request,
            api_key=api_key_from_request(request),
        )
        document.refresh_from_db()
        return Response(self.get_serializer(document).data)

    @action(detail=True, methods=["get"], url_path=r"versions/(?P<from_version>[0-9]+)/diff")
    def diff(self, request, pk=None, from_version=None):
        document = self.get_object()
        to_version = request.query_params.get("to")
        try:
            source = document.versions.get(version=int(from_version))
            target = document.versions.get(version=int(to_version)) if to_version else document.versions.first()
        except DocumentVersion.DoesNotExist as exc:
            raise ValidationError(str(exc)) from exc
        diff_lines = list(
            difflib.unified_diff(
                source.content.splitlines(),
                target.content.splitlines(),
                fromfile=f"v{source.version}",
                tofile=f"v{target.version}",
                lineterm="",
            )
        )
        return Response(
            {
                "from": source.version,
                "to": target.version,
                "diff": "\n".join(diff_lines),
            }
        )


# ---------------------------------------------------------------------------
# Files
# ---------------------------------------------------------------------------
class FileViewSet(CreatePermissionMixin, PermissionFilterMixin, viewsets.ModelViewSet):
    queryset = File.objects.select_related("resource", "workspace", "project")
    serializer_class = s.FileSerializer
    permission_classes = [IsAuthenticated, ResourcePermission]
    search_fields = ["name", "path"]

    def get_queryset(self):
        queryset = super().get_queryset()
        params = self.request.query_params
        if params.get("workspace"):
            queryset = queryset.filter(workspace_id=params["workspace"])
        if params.get("project"):
            queryset = queryset.filter(project_id=params["project"])
        return queryset

    def get_create_target(self, validated_data):
        project = validated_data.get("project")
        if project is not None:
            return project.resource
        workspace = validated_data.get("workspace")
        return workspace.resource if workspace else None

    def perform_destroy(self, instance):
        FileService.delete(
            stored_file=instance,
            user=self.request.user,
            request=self.request,
            api_key=api_key_from_request(self.request),
        )

    @action(
        detail=False,
        methods=["post"],
        url_path="upload",
        parser_classes=[MultiPartParser, FormParser],
    )
    def upload(self, request):
        upload = request.FILES.get("file")
        if upload is None:
            raise ValidationError({"file": "This field is required."})
        workspace = get_object_or_404(Workspace, pk=request.data.get("workspace"))
        project = None
        if request.data.get("project"):
            project = get_object_or_404(Project, pk=request.data["project"])
        target = project.resource if project else workspace.resource
        if not PermissionService.check(
            request.user, target, Permission.WRITE, api_key=api_key_from_request(request)
        ):
            raise PermissionDenied("You do not have write access to the target resource.")

        stored_file = FileService.create(
            workspace=workspace,
            project=project,
            name=upload.name,
            data=upload.read(),
            path=request.data.get("path") or None,
            created_by=request.user,
            request=request,
            api_key=api_key_from_request(request),
        )
        return Response(self.get_serializer(stored_file).data, status=status.HTTP_201_CREATED)

    @action(detail=True, methods=["get"])
    def download(self, request, pk=None):
        stored_file = self.get_object()
        data = FileService.read_bytes(stored_file)
        response = HttpResponse(data, content_type=stored_file.mime_type)
        response["Content-Disposition"] = f'attachment; filename="{stored_file.name}"'
        return response


# ---------------------------------------------------------------------------
# Accounts / groups
# ---------------------------------------------------------------------------
class UserViewSet(viewsets.ModelViewSet):
    permission_classes = [IsAuthenticated]
    search_fields = ["username", "email", "display_name"]
    ordering_fields = ["username", "date_joined"]

    def get_queryset(self):
        if self.request.user.is_superuser:
            return User.objects.all()
        return User.objects.filter(pk=self.request.user.pk)

    def get_serializer_class(self):
        if self.request.user.is_superuser:
            return s.UserSerializer
        return s.SelfUserSerializer

    def get_permissions(self):
        if self.action in {"create", "destroy"} and not self.request.user.is_superuser:
            return [IsAdminUser()]
        return [IsAuthenticated()]


class GroupViewSet(viewsets.ModelViewSet):
    queryset = Group.objects.all()
    serializer_class = s.GroupSerializer
    search_fields = ["name"]
    ordering_fields = ["name", "created_at"]

    def get_permissions(self):
        if self.action in {"create", "update", "partial_update", "destroy", "add_member"}:
            return [IsAdminUser()]
        return [IsAuthenticated()]

    @action(detail=True, methods=["post"], url_path="members")
    def add_member(self, request, pk=None):
        group = self.get_object()
        serializer = s.GroupMembershipSerializer(data={**request.data, "group": group.pk})
        serializer.is_valid(raise_exception=True)
        serializer.save()
        return Response(serializer.data, status=status.HTTP_201_CREATED)

    @action(detail=True, methods=["get"], url_path="members")
    def list_members(self, request, pk=None):
        group = self.get_object()
        data = s.GroupMembershipSerializer(group.memberships.all(), many=True).data
        return Response(data)


class ApiKeyViewSet(viewsets.ModelViewSet):
    permission_classes = [IsAuthenticated]

    def get_queryset(self):
        if self.request.user.is_superuser:
            return ApiKey.objects.all()
        return ApiKey.objects.filter(user=self.request.user)

    def get_serializer_class(self):
        if self.action == "create":
            return s.ApiKeyCreateSerializer
        return s.ApiKeySerializer

    def create(self, request, *args, **kwargs):
        serializer = s.ApiKeyCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        scopes = [dict(scope) for scope in serializer.validated_data.get("scopes", [])]
        api_key, raw_key = ApiKeyService.create(
            user=request.user,
            name=serializer.validated_data["name"],
            scopes=scopes,
            expires_at=serializer.validated_data.get("expires_at"),
            actor=request.user,
            request=request,
        )
        data = s.ApiKeySerializer(api_key, context=self.get_serializer_context()).data
        data["key"] = raw_key
        return Response(data, status=status.HTTP_201_CREATED)

    @action(detail=True, methods=["post"])
    def revoke(self, request, pk=None):
        api_key = self.get_object()
        api_key.revoke()
        return Response(s.ApiKeySerializer(api_key).data)


# ---------------------------------------------------------------------------
# Git
# ---------------------------------------------------------------------------
def _run_git(func, *args, **kwargs):
    try:
        return func(*args, **kwargs)
    except GitError as exc:
        raise ValidationError({"git": str(exc)}) from exc


class GitRepositoryViewSet(CreatePermissionMixin, PermissionFilterMixin, viewsets.ModelViewSet):
    queryset = GitRepository.objects.select_related(
        "resource", "workspace", "project", "sync_state"
    )
    serializer_class = s.GitRepositorySerializer
    permission_classes = [IsAuthenticated, ResourcePermission]
    search_fields = ["name", "remote_url"]
    ordering_fields = ["name", "created_at"]

    def get_queryset(self):
        queryset = super().get_queryset()
        params = self.request.query_params
        if params.get("workspace"):
            queryset = queryset.filter(workspace_id=params["workspace"])
        if params.get("project"):
            queryset = queryset.filter(project_id=params["project"])
        return queryset

    def get_create_target(self, validated_data):
        project = validated_data.get("project")
        if project is not None:
            return project.resource
        workspace = validated_data.get("workspace")
        return workspace.resource if workspace else None

    def perform_destroy(self, instance):
        GitService.detach_repository(instance, user=self.request.user, request=self.request)

    @action(detail=True, methods=["post"])
    def pull(self, request, pk=None):
        repository = self.get_object()
        result = _run_git(
            GitService.pull_repository, repository, user=request.user, request=request
        )
        return Response(result)

    @action(detail=True, methods=["post"])
    def push(self, request, pk=None):
        repository = self.get_object()
        _run_git(GitService.push_repository, repository, user=request.user, request=request)
        return Response({"pushed": True})

    @action(detail=True, methods=["post"])
    def commit(self, request, pk=None):
        repository = self.get_object()
        message = request.data.get("message") or "Manual commit"
        sha = _run_git(
            GitService.commit_repository,
            repository=repository,
            message=message,
            user=request.user,
            request=request,
        )
        return Response({"sha": sha})

    @action(detail=True, methods=["post"])
    def scan(self, request, pk=None):
        repository = self.get_object()
        result = _run_git(
            GitService.scan_repository, repository, user=request.user, request=request
        )
        return Response(result)

    @action(detail=True, methods=["get"])
    def status(self, request, pk=None):
        repository = self.get_object()
        return Response(GitService.status_repository(repository))

    @action(detail=True, methods=["get"])
    def branches(self, request, pk=None):
        repository = self.get_object()
        return Response({"branches": GitService._client(repository).branches()})

    @action(detail=True, methods=["get"])
    def diff(self, request, pk=None):
        repository = self.get_object()
        from_ref = request.query_params.get("from")
        if not from_ref:
            raise ValidationError({"from": "This query parameter is required."})
        return Response({"diff": GitService.diff_repository(repository, from_ref, request.query_params.get("to", ""))})


# ---------------------------------------------------------------------------
# Audit
# ---------------------------------------------------------------------------
class AuditEventViewSet(viewsets.ReadOnlyModelViewSet):
    serializer_class = s.AuditEventSerializer
    permission_classes = [IsAuthenticated]
    ordering_fields = ["timestamp"]

    def get_queryset(self):
        queryset = AuditEvent.objects.select_related("user", "resource")
        if not self.request.user.is_superuser:
            queryset = queryset.filter(user=self.request.user)
        params = self.request.query_params
        if params.get("action"):
            queryset = queryset.filter(action=params["action"])
        if params.get("resource"):
            queryset = queryset.filter(resource_id=params["resource"])
        return queryset
