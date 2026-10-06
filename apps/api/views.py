"""REST API views. All object access is funneled through the permission engine."""

from __future__ import annotations

import difflib
from pathlib import Path

from django.contrib.auth import get_user_model
from django.core.exceptions import PermissionDenied as DjangoPermissionDenied
from django.core.exceptions import ValidationError as DjangoValidationError
from django.http import HttpResponse, StreamingHttpResponse
from django.shortcuts import get_object_or_404
from rest_framework import filters, status, viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import PermissionDenied, ValidationError
from rest_framework.parsers import FormParser, MultiPartParser
from rest_framework.permissions import AllowAny, IsAdminUser, IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.accounts.models import ApiKey
from apps.accounts.services import ApiKeyService
from apps.audit.models import AuditAction, AuditEvent, AuditSource
from apps.audit.services import AuditService
from apps.curator.models import CuratorProposal
from apps.deadlines.models import KnowledgeDeadline
from apps.documents.models import (
    Document,
    DocumentComment,
    DocumentFolder,
    DocumentStatus,
    DocumentVersion,
)
from apps.documents.services import DocumentService
from apps.files.models import File
from apps.files.services import FileService
from apps.gateway.models import GatewayTarget
from apps.git.git_cli import GitError
from apps.git.models import GitCredential, GitRepository
from apps.git.services import GitService
from apps.groups.models import Group
from apps.knowledge.services import DiscoveryService, DraftService, GraphService, QualityService
from apps.links.models import ResourceLink
from apps.links.services import LinkService
from apps.permissions.constants import Effect, Permission
from apps.permissions.models import ResourceACL
from apps.permissions.services import PermissionService
from apps.resources.models import Resource
from apps.search.services import SearchService
from apps.secrets.models import Secret
from apps.secrets.services import SecretService
from apps.settings_store.services import describe
from apps.workspaces.models import Project, Workspace
from apps.workspaces.ownership import OwnershipService
from apps.workspaces.services import WorkspaceService

from . import serializers as s
from .llm_guide import build_manifest
from .permissions import (
    GroupAdminPermission,
    PermissionFilterMixin,
    RelatedResourcePermission,
    ResourceACLPermission,
    ResourcePermission,
    SuperuserOnly,
    api_key_from_request,
)

User = get_user_model()

#: Answer for every ownership refusal, so the four REST paths (workspace/project
#: x transfer/takeover) are identical and never echo the engine's wording.
_OWNERSHIP_DENIED = "Nincs jogosultságod ehhez a művelethez."


def _first_message(exc: DjangoValidationError) -> str:
    messages = getattr(exc, "messages", None) or [str(exc)]
    return str(messages[0])


def _resolve_new_owner(request):
    """The user id in the payload, as a row. 400 when absent or unknown."""
    raw = request.data.get("new_owner")
    if not raw:
        raise ValidationError({"new_owner": "Ez a mező kötelező."})
    new_owner = User.objects.filter(pk=raw).first()
    if new_owner is None:
        raise ValidationError({"new_owner": "Nincs ilyen felhasználó."})
    return new_owner


class CreatePermissionMixin:
    """Checks write/admin permission against the create target resource."""

    create_permission = Permission.WRITE

    #: Payload keys whose value *is* a Resource id (workspace, project, resource,
    #: link source/target - they are all resource-backed with the Resource as the
    #: primary key), most specific first. Empty means "no resource in the payload".
    create_target_fields: tuple[str, ...] = ()

    def get_create_target(self, validated_data):
        return None

    def _require_write(self, obj, request, permission=Permission.WRITE):
        resource = getattr(obj, "resource", None) or obj
        if not PermissionService.check(
            request.user, resource, permission, api_key=api_key_from_request(request)
        ):
            raise PermissionDenied("Write permission required on this resource.")

    def _require_readable_payload(self, request):
        """403 when a resource named in the payload is not readable.

        The serializers now reject an id the caller may not READ with a "does not
        exist" validation error - that is what closes the existence oracle, since
        an unreadable id and a nonexistent one are then indistinguishable. This
        check runs *before* validation so a create that is simply not allowed
        still answers 403 (the answer the manifest tells an agent not to retry)
        instead of degrading into a 400 about a workspace the caller cannot even
        see. Unknown ids take the same branch: ``check`` is False for a missing
        resource, so nothing here distinguishes the two cases either.
        """
        if not self.create_target_fields:
            return
        api_key = api_key_from_request(request)
        for field in self.create_target_fields:
            raw = request.data.get(field)
            if not raw:
                continue
            resource = Resource.objects.filter(pk=raw).first()
            if not PermissionService.check(
                request.user, resource, Permission.READ, api_key=api_key
            ):
                raise PermissionDenied("You do not have permission to create this resource.")
            return

    def create(self, request, *args, **kwargs):
        self._require_readable_payload(request)
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

    def partial_update(self, request, *args, **kwargs):
        """Rename / re-describe / re-slug, through the service.

        A plain ModelViewSet write would bypass both: the ACL and the audit
        trail. A ``slug`` change breaks links shared under the old one - there is
        no redirect table - and the audit entry flags it with ``broke_links``.
        """
        workspace = self.get_object()
        data = request.data
        try:
            WorkspaceService.update(
                workspace=workspace,
                name=data.get("name"),
                description=data.get("description"),
                slug=data.get("slug"),
                actor=request.user,
                request=request,
            )
        except DjangoPermissionDenied as exc:
            raise PermissionDenied(str(exc)) from exc
        except DjangoValidationError as exc:
            raise ValidationError(exc.message_dict) from exc
        workspace.refresh_from_db()
        return Response(self.get_serializer(workspace).data)

    @action(detail=True, methods=["post"])
    def transfer(self, request, pk=None):
        """Hand this workspace to somebody else.

        Ownership is not a shareable grant: the engine deliberately keeps it
        out of ``grant`` so it can never be escalated, which is why the only
        way to change it is this audited service call.
        """
        workspace = self.get_object()
        new_owner = _resolve_new_owner(request)
        try:
            OwnershipService.transfer(
                resource=workspace.resource,
                new_owner=new_owner,
                actor=request.user,
                keep_old_access=request.data.get("keep_old_access") or None,
                request=request,
            )
        except DjangoPermissionDenied as exc:
            raise PermissionDenied(_OWNERSHIP_DENIED) from exc
        except DjangoValidationError as exc:
            raise ValidationError({"detail": _first_message(exc)}) from exc
        workspace.refresh_from_db()
        return Response(self.get_serializer(workspace).data)

    @action(detail=True, methods=["post"])
    def takeover(self, request, pk=None):
        """The audited way in for a superuser who has no access at all.

        It deliberately does not go through ``get_object()``: that would need
        READ on the workspace, which is exactly what the caller does not have.
        ``can_take_over`` (inside the service) is the gate, and it refuses
        anything but a superuser, so the row is only resolved once the caller
        has proved it is one.
        """
        if not request.user.is_superuser:
            raise PermissionDenied(_OWNERSHIP_DENIED)
        workspace = get_object_or_404(Workspace, pk=pk)
        try:
            OwnershipService.take_over(
                resource=workspace.resource, actor=request.user, request=request
            )
        except DjangoPermissionDenied as exc:
            raise PermissionDenied(_OWNERSHIP_DENIED) from exc
        return Response(self.get_serializer(workspace).data)


class ProjectViewSet(CreatePermissionMixin, PermissionFilterMixin, viewsets.ModelViewSet):
    queryset = Project.objects.select_related("resource", "workspace")
    serializer_class = s.ProjectSerializer
    permission_classes = [IsAuthenticated, ResourcePermission]
    search_fields = ["name", "slug"]
    ordering_fields = ["name", "created_at"]
    create_target_fields = ("workspace",)

    def get_queryset(self):
        queryset = super().get_queryset()
        workspace_id = self.request.query_params.get("workspace")
        if workspace_id:
            queryset = queryset.filter(workspace_id=workspace_id)
        return queryset

    def get_create_target(self, validated_data):
        workspace = validated_data.get("workspace")
        return workspace.resource if workspace else None

    @action(detail=True, methods=["post"])
    def transfer(self, request, pk=None):
        project = self.get_object()
        new_owner = _resolve_new_owner(request)
        try:
            OwnershipService.transfer(
                resource=project.resource,
                new_owner=new_owner,
                actor=request.user,
                keep_old_access=request.data.get("keep_old_access") or None,
                request=request,
            )
        except DjangoPermissionDenied as exc:
            raise PermissionDenied(_OWNERSHIP_DENIED) from exc
        except DjangoValidationError as exc:
            raise ValidationError({"detail": _first_message(exc)}) from exc
        project.refresh_from_db()
        return Response(self.get_serializer(project).data)

    @action(detail=True, methods=["post"])
    def takeover(self, request, pk=None):
        if not request.user.is_superuser:
            raise PermissionDenied(_OWNERSHIP_DENIED)
        project = get_object_or_404(Project, pk=pk)
        try:
            OwnershipService.take_over(
                resource=project.resource, actor=request.user, request=request
            )
        except DjangoPermissionDenied as exc:
            raise PermissionDenied(_OWNERSHIP_DENIED) from exc
        return Response(self.get_serializer(project).data)


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


class ResourceACLViewSet(CreatePermissionMixin, viewsets.ModelViewSet):
    queryset = ResourceACL.objects.select_related("resource")
    serializer_class = s.ResourceACLSerializer
    permission_classes = [IsAuthenticated, ResourceACLPermission]
    create_permission = Permission.ADMIN
    create_target_fields = ("resource",)

    def get_queryset(self):
        queryset = super().get_queryset()
        resource_id = self.request.query_params.get("resource")
        if resource_id:
            queryset = queryset.filter(resource_id=resource_id)
        return queryset

    def filter_queryset(self, queryset):
        """Scope to the resources whose ACL this caller may manage.

        ``can_manage_acl`` is the engine's own answer to "who may change this
        ACL", and it is the identical question the web UI's access panel asks -
        so the two surfaces cannot drift apart, and a delegated admin sees
        exactly the entries they can change there.
        """
        queryset = super().filter_queryset(queryset)
        resources = {
            row.id: row
            for row in Resource.objects.filter(
                id__in=queryset.values_list("resource_id", flat=True)
            ).select_related("parent")
        }
        manageable = [
            resource_id
            for resource_id, resource in resources.items()
            if PermissionService.can_manage_acl(self.request.user, resource)
        ]
        return queryset.filter(resource_id__in=manageable)

    def get_create_target(self, validated_data):
        return validated_data.get("resource")

    @staticmethod
    def _acl_values(serializer, instance=None):
        """The resulting entry, whichever fields the caller left out."""
        data = serializer.validated_data
        get = data.get
        return {
            "resource": get("resource", getattr(instance, "resource", None)),
            "subject_type": get("subject_type", getattr(instance, "subject_type", None)),
            "subject_id": get("subject_id", getattr(instance, "subject_id", None)),
            "permission": get("permission", getattr(instance, "permission", None)),
            "effect": get("effect", getattr(instance, "effect", Effect.ALLOW)),
            "inherit": get("inherit", getattr(instance, "inherit", True)),
        }

    def perform_create(self, serializer):
        """Write through the validated engine path, not straight to the table.

        ``grant`` checks the permission level *and* the subject against
        ``grantable_subjects``, which is what keeps this collection from
        becoming a way around the rules the web UI and the MCP surface enforce:
        the REST caller cannot grant more than it holds, cannot share outside
        its own work group, and cannot touch a personal workspace.
        """
        values = self._acl_values(serializer)
        serializer.instance = PermissionService.grant(
            values.pop("resource"),
            created_by=self.request.user,
            **values,
        )

    def perform_update(self, serializer):
        """Re-validate through ``grant`` before writing.

        The same argument as ``perform_create`` applies with more force: a
        PATCH that flips ``effect`` to ``deny`` or swaps the subject is a
        grant, and it must not be able to walk past the checks a POST is held
        to. ``grant`` upserts on (resource, subject, permission), so when the
        request *moved* the entry the old row is dropped afterwards - otherwise
        it would keep granting exactly what the caller just took away.
        """
        instance = serializer.instance
        values = self._acl_values(serializer, instance)
        resource = values.pop("resource")
        entry = PermissionService.grant(resource, created_by=self.request.user, **values)
        if entry.pk != instance.pk:
            PermissionService.revoke(
                instance.resource,
                subject_type=instance.subject_type,
                subject_id=instance.subject_id,
                permission=instance.permission,
                actor=self.request.user,
            )
        serializer.instance = entry


class ResourceLinkViewSet(CreatePermissionMixin, PermissionFilterMixin, viewsets.ModelViewSet):
    queryset = ResourceLink.objects.select_related("source", "target")
    serializer_class = s.ResourceLinkSerializer
    permission_classes = [IsAuthenticated, RelatedResourcePermission]
    resource_field = "source_id"
    create_target_fields = ("source", "target")

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
        # The *source* is the side that gets modified, so it carries the
        # permission: read to see the link, write to create or retarget it,
        # delete to remove it. The target is a second, separate check - see
        # ``perform_create`` and ``filter_queryset``.
        if action in {"create", "update", "partial_update"}:
            return Permission.WRITE
        if action == "destroy":
            return Permission.DELETE
        return Permission.READ

    def get_create_target(self, validated_data):
        return validated_data.get("source")

    def filter_queryset(self, queryset):
        """Also require READ on the target of every listed link.

        A link row names its other end (``target_name`` is serialised), so a
        list filtered on the source alone would be a directory of resource names
        the caller has no access to - and a retrieve of such a link would be a
        one-row version of the same leak.
        """
        queryset = super().filter_queryset(queryset)
        readable = PermissionService.allowed_resource_ids(
            self.request.user,
            queryset.values_list("target_id", flat=True),
            Permission.READ,
            api_key=api_key_from_request(self.request),
        )
        return queryset.filter(target_id__in=readable)

    def perform_create(self, serializer):
        """Creating a link needs READ on both ends, not just on the source.

        Without the target check any authenticated caller could link any two
        resources by id: a write into a workspace they cannot even see, and an
        existence oracle for every resource id in the instance.
        """
        target = serializer.validated_data.get("target")
        if not PermissionService.check(
            self.request.user,
            target,
            Permission.READ,
            api_key=api_key_from_request(self.request),
        ):
            raise PermissionDenied("Read access required on the link target.")
        serializer.save()

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
    create_target_fields = ("project", "workspace")

    def get_queryset(self):
        # folder__project is loaded so the serialised tree_path does not walk a
        # query per document.
        queryset = super().get_queryset().select_related(
            "folder__project", "project", "resource"
        )
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

    @action(detail=False, methods=["post"], url_path="bulk", parser_classes=[MultiPartParser, FormParser])
    def bulk(self, request):
        """Create many documents at once from uploaded markdown files."""
        from apps.documents.services import DocumentService

        workspace = get_object_or_404(Workspace, pk=request.data.get("workspace"))
        project = (
            get_object_or_404(Project, pk=request.data["project"])
            if request.data.get("project")
            else None
        )
        target = project.resource if project else workspace.resource
        if not PermissionService.check(
            request.user, target, Permission.WRITE, api_key=api_key_from_request(request)
        ):
            raise PermissionDenied("Write permission required on the target.")
        uploads = [(f.name, f.read()) for f in request.FILES.getlist("files")]
        if not uploads:
            raise ValidationError({"files": "No files supplied."})
        folder = (request.data.get("folder") or "").strip()
        result = DocumentService.bulk_create_from_files(
            workspace=workspace,
            project=project,
            folder=folder,
            uploads=uploads,
            created_by=request.user,
            request=request,
            api_key=api_key_from_request(request),
        )
        return Response(
            {
                "created": [s.DocumentSerializer(doc).data for doc in result["created"]],
                "skipped": result["skipped"],
            },
            status=status.HTTP_201_CREATED,
        )

    @action(detail=False, methods=["post"], url_path="from-template")
    def from_template(self, request):
        """Instantiate a new document from a template."""
        from apps.documents.services import DocumentService

        template = get_object_or_404(Document, pk=request.data.get("template_id"))
        workspace = get_object_or_404(Workspace, pk=request.data.get("workspace"))
        project = (
            get_object_or_404(Project, pk=request.data["project"])
            if request.data.get("project")
            else None
        )
        target = project.resource if project else workspace.resource
        if not PermissionService.check(
            request.user, target, Permission.WRITE, api_key=api_key_from_request(request)
        ):
            raise PermissionDenied("Write permission required on the target.")
        document = DocumentService.instantiate_template(
            template=template,
            workspace=workspace,
            project=project,
            title=request.data.get("title", ""),
            path=request.data.get("path", ""),
            folder=request.data.get("folder", ""),
            created_by=request.user,
            request=request,
            api_key=api_key_from_request(request),
        )
        return Response(
            s.DocumentSerializer(document).data, status=status.HTTP_201_CREATED
        )

    @action(detail=True, methods=["post", "patch"], url_path="move")
    def move(self, request, pk=None):
        """Move a document to another folder (path or folder prefix)."""
        from apps.documents.services import DocumentService

        document = self.get_object()
        if not PermissionService.check(
            request.user, document.resource, Permission.WRITE, api_key=api_key_from_request(request)
        ):
            raise PermissionDenied("Write permission required.")
        new_path = request.data.get("path")
        node = (request.data.get("node") or "").strip("/")
        if node:
            # Additive node addressing: the whole workspace-relative path, the
            # same one the web pages use, instead of project + scope path.
            from apps.documents.folders import folder_by_tree_path, project_of_node

            destination = folder_by_tree_path(document.workspace, node)
            if destination is None:
                raise ValidationError({"node": f"Nincs ilyen csomópont: {node}"})
            dest_project = project_of_node(destination)
            if (dest_project.pk if dest_project else None) != document.project_id:
                raise ValidationError({"node": "Csak a saját projektjén belül mozgatható."})
            parent = destination.path
            new_path = (
                f"{parent}/{Path(document.path).name}"
                if parent
                else Path(document.path).name
            )
        if not new_path:
            folder = (request.data.get("folder") or "").strip("/")
            new_path = f"{folder}/{Path(document.path).name}" if folder else Path(document.path).name
        DocumentService.move(
            document, new_path, user=request.user, request=request,
            api_key=api_key_from_request(request),
        )
        return Response(self.get_serializer(document).data)

    @action(detail=True, methods=["get"])
    def related(self, request, pk=None):
        document = self.get_object()
        try:
            depth = int(request.query_params.get("depth", 1))
        except (TypeError, ValueError):
            depth = 1
        results = GraphService.neighbors(
            request.user,
            document.resource,
            api_key=api_key_from_request(request),
            depth=depth,
        )
        return Response({"related": results, "count": len(results)})

    def _set_status(self, request, status):
        document = self.get_object()
        self._require_write(document, request)
        updated = DraftService.set_status(
            document=document,
            status=status,
            user=request.user,
            request=request,
            api_key=api_key_from_request(request),
        )
        return Response(self.get_serializer(updated).data)

    @action(detail=True, methods=["post"])
    def approve(self, request, pk=None):
        return self._set_status(request, DocumentStatus.APPROVED)

    @action(detail=True, methods=["post"])
    def reject(self, request, pk=None):
        return self._set_status(request, DocumentStatus.DRAFT)


# ---------------------------------------------------------------------------
# Files
# ---------------------------------------------------------------------------
class FileViewSet(CreatePermissionMixin, PermissionFilterMixin, viewsets.ModelViewSet):
    queryset = File.objects.select_related(
        "resource", "workspace", "project", "folder__project"
    )
    serializer_class = s.FileSerializer
    permission_classes = [IsAuthenticated, ResourcePermission]
    search_fields = ["name", "path"]
    create_target_fields = ("project", "workspace")

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
class UserQSearchFilter(filters.SearchFilter):
    """DRF's ``SearchFilter`` reads ``?search=``, but this endpoint's contract
    (and the access panel that calls it) is ``?q=``.

    Without this the query parameter was silently ignored, so ``?q=<anything>``
    returned the *whole* user table to any authenticated caller -- the directory
    gate in ``list()`` only required the parameter to be present.
    """

    search_param = "q"


class UserViewSet(viewsets.ModelViewSet):
    permission_classes = [IsAuthenticated]
    search_fields = ["username", "email", "display_name"]
    ordering_fields = ["username", "date_joined"]
    filter_backends = [UserQSearchFilter, filters.OrderingFilter]

    #: Actions that mutate a row. The access panel needs a *directory* lookup to
    #: find users worth granting access to, but a non-superuser may only ever
    #: write their own row.
    WRITE_ACTIONS = {"update", "partial_update"}

    def get_queryset(self):
        user = self.request.user
        if user.is_superuser:
            return User.objects.all()
        # Scoped on write. Without this, SelfUserSerializer -- which keeps
        # `password` writable so people can change their own -- also let any
        # authenticated user rewrite any other account's password.
        if getattr(self, "action", None) in self.WRITE_ACTIONS:
            return User.objects.filter(pk=user.pk)
        return User.objects.all()

    def get_serializer_class(self):
        if self.request.user.is_superuser:
            return s.UserSerializer
        return s.SelfUserSerializer

    def get_permissions(self):
        if self.action in {"create", "destroy"} and not self.request.user.is_superuser:
            return [IsAdminUser()]
        return [IsAuthenticated()]

    def get_serializer(self, *args, **kwargs):
        # Directory callers (write/admin on some resource) may look up users
        # to grant them access, but not edit them.
        serializer_class = self.get_serializer_class()
        kwargs.setdefault("context", self.get_serializer_context())
        return serializer_class(*args, **kwargs)

    def list(self, request, *args, **kwargs):
        # Non-superusers get a read-only directory (id/username/display_name)
        # only when they search; a full list is staff-only.
        if not request.user.is_superuser and not request.query_params.get("q"):
            return Response(
                {"detail": "Provide ?q=<term> to search the user directory, or log in as staff to list all."},
                status=status.HTTP_403_FORBIDDEN,
            )
        return super().list(request, *args, **kwargs)

    def destroy(self, request, *args, **kwargs):
        """Delete a user, or explain what still blocks it.

        ``Workspace.owner``/``Project.owner`` are PROTECT, so a user who owns
        shared content cannot be deleted. Answering 409 with the list is the
        point: the bare ``ProtectedError`` is a 500 and tells an operator
        nothing. Their Personal workspace goes with them - one holder, no
        meaning without them.
        """
        from apps.accounts.services import OwnerConflict, UserService

        user = self.get_object()
        try:
            UserService.delete(user=user, actor=request.user, request=request)
        except OwnerConflict as exc:
            return Response(exc.detail, status=status.HTTP_409_CONFLICT)
        return Response(status=status.HTTP_204_NO_CONTENT)

    @action(detail=True, methods=["post"])
    def deactivate(self, request, *args, **kwargs):
        """Offboard without data loss: stop the account, keep the rows.

        This is what "remove this user" usually means, and with OIDC it is the
        only correct operation - the identity provider is the source of truth and
        the local row mirrors it. Also revokes the user's live API keys and drops
        superuser/staff, so a deactivated account cannot keep control-plane power.
        """
        from apps.accounts.services import UserService

        user = self.get_object()
        if user.pk == request.user.pk:
            return Response(
                {"detail": "You cannot deactivate your own account."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        UserService.deactivate(user=user, actor=request.user, request=request)
        return Response({"id": str(user.pk), "username": user.username, "is_active": False})

    @action(detail=True, methods=["get"])
    def ownership(self, request, *args, **kwargs):
        """What this user owns, so an operator can see why a delete is blocked."""
        from apps.accounts.services import UserService

        user = self.get_object()
        workspaces, projects = UserService.owned_by(user)
        return Response(
            {
                "can_delete": not workspaces and not projects,
                "owned_workspaces": [
                    {"id": str(ws.pk), "name": ws.name, "slug": ws.slug, "kind": ws.kind}
                    for ws in workspaces
                ],
                "owned_projects": [
                    {"id": str(pr.pk), "name": pr.name} for pr in projects
                ],
            }
        )


class GroupViewSet(viewsets.ModelViewSet):
    """Knowledge groups and their membership.

    Administration is deliberately narrow. Creating a group is a superuser-only
    move, and changing the group or its members belongs to that group's
    managers (``GroupMembership.Role.MANAGER``) or a superuser. ``is_staff``
    gets nothing here: a group that is granted access on some resource hands
    that access to every member, so "add me to this group" is an escalation,
    not administration.
    """

    queryset = Group.objects.all()
    serializer_class = s.GroupSerializer
    search_fields = ["name"]
    ordering_fields = ["name", "created_at"]
    #: Reads are open to any authenticated caller; ``get_permissions`` narrows
    #: every write. Declared here so the /llm manifest reports it truthfully
    #: instead of falling back to "default".
    permission_classes = [IsAuthenticated]

    def get_permissions(self):
        if self.action == "create":
            return [SuperuserOnly()]
        if self.action in {"update", "partial_update", "destroy"}:
            return [GroupAdminPermission()]
        if self.action == "members" and self.request.method == "POST":
            return [GroupAdminPermission()]
        return [IsAuthenticated()]

    @action(detail=True, methods=["get", "post"], url_path="members")
    def members(self, request, pk=None):
        group = self.get_object()
        if request.method == "POST":
            serializer = s.GroupMembershipSerializer(
                data={**request.data, "group": group.pk}
            )
            serializer.is_valid(raise_exception=True)
            serializer.save()
            return Response(serializer.data, status=status.HTTP_201_CREATED)
        return Response(s.GroupMembershipSerializer(group.memberships.all(), many=True).data)


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
        if api_key_from_request(request) is not None:
            # An agent key must not be able to mint a permanent credential for its
            # owner: a scoped key could bootstrap an unscoped one and shed the
            # scope it was given. Key management stays a human, session-only action.
            raise PermissionDenied(
                "An API key cannot create another API key; sign in with a session."
            )
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
    create_target_fields = ("project", "workspace")

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

    @action(detail=True, methods=["get", "put", "delete"], url_path="credential")
    def credential(self, request, pk=None):
        """Register the caller's own PAT for this repository.

        A user may always manage their own credential. Acting on *another*
        user's credential is a superuser-only, audited break-glass: it hands one
        account a push credential of another, so it is not something ``is_staff``
        may do - staff is not a trust level here, and a staff user with write
        access to a repository could otherwise borrow somebody else's token.
        With a personal credential the push uses the caller's own PAT (no
        co-author trailer); without it the repository/global credential is used
        and its owner is recorded as a co-author.
        """
        from apps.accounts.models import User as UserModel
        from apps.secrets.models import Secret as SecretModel

        repository = self.get_object()
        if not PermissionService.check(
            request.user, repository.resource, Permission.WRITE
        ):
            raise PermissionDenied("Write access required on the repository.")

        target_user = request.user
        if request.query_params.get("user"):
            if not request.user.is_superuser:
                raise PermissionDenied(
                    "Csak superuser kezelheti más felhasználó hitelesítő adatait."
                )
            target_user = get_object_or_404(UserModel, pk=request.query_params["user"])
            AuditService.log(
                AuditAction.CHANGE_PERMISSION,
                user=request.user,
                resource=repository.resource,
                source=AuditSource.API,
                request=request,
                detail={"type": "git_credential", "target_user": str(target_user.pk)},
            )

        if request.method == "DELETE":
            GitCredential.objects.filter(repository=repository, user=target_user).delete()
            return Response(status=status.HTTP_204_NO_CONTENT)

        secret_id = (request.data or {}).get("secret")
        if not secret_id:
            raise ValidationError({"secret": "This field is required."})
        secret = get_object_or_404(SecretModel, pk=secret_id, owner=target_user)
        credential = GitService.set_user_credential(
            repository=repository, user=target_user, secret=secret
        )
        return Response(
            {
                "id": str(credential.pk),
                "user": str(credential.user_id),
                "repository": str(repository.pk),
                "secret": str(credential.secret_id),
                "secret_name": credential.secret.name,
            },
            status=status.HTTP_201_CREATED,
        )

    @action(detail=True, methods=["get"], url_path="credentials")
    def credentials(self, request, pk=None):
        repository = self.get_object()
        rows = GitCredential.objects.filter(repository=repository).select_related(
            "user", "secret"
        )
        return Response(
            {
                "credentials": [
                    {
                        "id": str(row.pk),
                        "user": str(row.user_id),
                        "username": row.user.username,
                        "secret": str(row.secret_id),
                        "secret_name": row.secret.name,
                    }
                    for row in rows
                ]
            }
        )

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
# Secrets
# ---------------------------------------------------------------------------
class SecretViewSet(viewsets.ModelViewSet):
    permission_classes = [IsAuthenticated]
    search_fields = ["name"]

    def get_queryset(self):
        return Secret.objects.filter(owner=self.request.user).prefetch_related("attachments")

    def get_serializer_class(self):
        if self.action == "create":
            return s.SecretCreateSerializer
        return s.SecretSerializer

    def create(self, request, *args, **kwargs):
        serializer = s.SecretCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        secret = SecretService.create(
            owner=request.user,
            name=data["name"],
            payload=data.get("payload"),
            secret_type=data.get("secret_type", "generic"),
            description=data.get("description", ""),
            metadata=data.get("metadata") or {},
            request=request,
        )
        return Response(s.SecretSerializer(secret).data, status=status.HTTP_201_CREATED)

    def update(self, request, *args, **kwargs):
        secret = self.get_object()
        SecretService.update(
            secret=secret,
            payload=request.data.get("payload", None),
            description=request.data.get("description", None),
            metadata=request.data.get("metadata", None),
            secret_type=request.data.get("secret_type"),
            request=request,
        )
        return Response(s.SecretSerializer(secret).data)

    def _target(self, request):
        workspace = None
        project = None
        if request.data.get("workspace"):
            workspace = get_object_or_404(Workspace, pk=request.data["workspace"])
        if request.data.get("project"):
            project = get_object_or_404(Project, pk=request.data["project"])
        return workspace, project

    @action(detail=True, methods=["post"])
    def rotate(self, request, pk=None):
        secret = self.get_object()
        if "payload" not in request.data:
            raise ValidationError({"payload": "This field is required."})
        SecretService.update(secret=secret, payload=request.data["payload"], request=request)
        return Response(s.SecretSerializer(secret).data)

    @action(detail=True, methods=["post"])
    def attach(self, request, pk=None):
        secret = self.get_object()
        workspace, project = self._target(request)
        attachment = SecretService.attach(
            secret=secret, workspace=workspace, project=project, created_by=request.user
        )
        return Response(
            s.SecretAttachmentSerializer(attachment).data, status=status.HTTP_201_CREATED
        )

    @action(detail=True, methods=["post"])
    def detach(self, request, pk=None):
        secret = self.get_object()
        workspace, project = self._target(request)
        count = SecretService.detach(secret=secret, workspace=workspace, project=project)
        return Response({"detached": count})

    @action(detail=True, methods=["post"])
    def use(self, request, pk=None):
        secret = self.get_object()
        try:
            value = SecretService.reveal(
                secret,
                user=request.user,
                workspace_id=request.data.get("workspace"),
                project_id=request.data.get("project"),
                request=request,
                api_key=api_key_from_request(request),
            )
        except PermissionDenied:
            raise
        except Exception as exc:  # noqa: BLE001
            raise PermissionDenied(str(exc)) from exc
        return Response({"id": str(secret.pk), "name": secret.name, "value": value})


class GatewayTargetViewSet(CreatePermissionMixin, PermissionFilterMixin, viewsets.ModelViewSet):
    """Named egress targets: external services reached through Brainbox.

    A caller needs ``Permission.USE`` on the target's Resource, not WRITE - using
    a target is not editing it. The ACL is the same one every resource carries,
    so who may reach an external service is a grant, and every call is audited.
    """

    serializer_class = s.GatewayTargetSerializer
    permission_classes = [IsAuthenticated, ResourcePermission]
    resource_field = "resource_id"
    search_fields = ["name", "base_url"]
    create_target_fields = ("project", "workspace")

    def get_queryset(self):
        queryset = GatewayTarget.objects.select_related("resource", "workspace", "project")
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

    @action(detail=True, methods=["post"], url_path="call")
    def call(self, request, pk=None):
        from rest_framework.exceptions import Throttled

        from apps.gateway.services import GatewayService, RateLimited

        # Use, not write: calling a target is not editing it.
        self.required_permission = Permission.USE
        target = self.get_object()
        if request.query_params.get("stream"):
            try:
                status, headers, chunks = GatewayService.stream(
                    target,
                    method=request.data.get("method", "GET"),
                    path=request.data.get("path", ""),
                    body=request.data.get("body"),
                    headers=request.data.get("headers"),
                    user=request.user,
                    request=request,
                    api_key=api_key_from_request(request),
                )
            except RateLimited as exc:
                raise Throttled(wait=exc.retry_after, detail=str(exc)) from exc
            except DjangoPermissionDenied as exc:
                raise PermissionDenied(str(exc)) from exc
            except DjangoValidationError as exc:
                detail = getattr(exc, "message_dict", None) or {"detail": exc.messages}
                raise ValidationError(detail) from exc
            content_type = next(
                (value for key, value in headers.items() if key.lower() == "content-type"),
                "application/octet-stream",
            )
            response = StreamingHttpResponse(chunks, status=status)
            response["Content-Type"] = content_type
            return response
        try:
            result = GatewayService.call(
                target,
                method=request.data.get("method", "GET"),
                path=request.data.get("path", ""),
                body=request.data.get("body"),
                headers=request.data.get("headers"),
                user=request.user,
                request=request,
                api_key=api_key_from_request(request),
            )
        except RateLimited as exc:
            raise Throttled(wait=exc.retry_after, detail=str(exc)) from exc
        except DjangoPermissionDenied as exc:
            raise PermissionDenied(str(exc)) from exc
        except DjangoValidationError as exc:
            detail = getattr(exc, "message_dict", None) or {"detail": exc.messages}
            raise ValidationError(detail) from exc
        return Response(result)


class CommentViewSet(viewsets.ModelViewSet):
    """Comments on documents. Visibility is the document's own.

    Any reader may comment; resolving or deleting needs to be the author or to
    hold write on the document. The queryset is filtered to readable documents,
    so an unreadable comment is a 404, not a hint that it exists.
    """

    serializer_class = s.DocumentCommentSerializer
    permission_classes = [IsAuthenticated]
    http_method_names = ["get", "post", "delete", "head", "options"]

    def get_queryset(self):
        queryset = DocumentComment.objects.select_related("document", "author")
        document_id = self.request.query_params.get("document")
        if document_id:
            queryset = queryset.filter(document_id=document_id)
        resource_ids = list(queryset.values_list("document__resource_id", flat=True))
        readable = PermissionService.allowed_resource_ids(
            self.request.user,
            resource_ids,
            Permission.READ,
            api_key=api_key_from_request(self.request),
        )
        return queryset.filter(document__resource_id__in=readable)

    def perform_destroy(self, instance):
        from apps.documents.comments import CommentService

        CommentService.delete(
            comment=instance,
            user=self.request.user,
            request=self.request,
            api_key=api_key_from_request(self.request),
        )

    def _set_resolved(self, request, *, resolved):
        from apps.documents.comments import CommentService

        comment = self.get_object()
        try:
            CommentService.set_resolved(
                comment=comment,
                user=request.user,
                resolved=resolved,
                request=request,
                api_key=api_key_from_request(request),
            )
        except DjangoPermissionDenied as exc:
            raise PermissionDenied(str(exc)) from exc
        comment.refresh_from_db()
        return Response(self.get_serializer(comment).data)

    @action(detail=True, methods=["post"])
    def resolve(self, request, pk=None):
        return self._set_resolved(request, resolved=True)

    @action(detail=True, methods=["post"])
    def reopen(self, request, pk=None):
        return self._set_resolved(request, resolved=False)


class CuratorProposalViewSet(PermissionFilterMixin, viewsets.ReadOnlyModelViewSet):
    """Review queue for curator proposals.

    Read on the proposal's resource to see it; write to decide. Approving applies
    the change through the same services the rest of the platform uses, so
    nothing here bypasses the permission engine or the audit trail.
    """

    serializer_class = s.CuratorProposalSerializer
    permission_classes = [IsAuthenticated, ResourcePermission]
    resource_field = "resource_id"
    search_fields = ["title", "signature"]

    def get_queryset(self):
        queryset = CuratorProposal.objects.select_related(
            "workspace", "resource", "decided_by"
        )
        params = self.request.query_params
        if params.get("workspace"):
            queryset = queryset.filter(workspace_id=params["workspace"])
        if params.get("status"):
            queryset = queryset.filter(status=params["status"])
        return queryset

    def _decide(self, request, *, approve):
        from apps.curator.services import CuratorService

        proposal = self.get_object()
        try:
            CuratorService.decide(
                proposal,
                approve=approve,
                user=request.user,
                request=request,
                api_key=api_key_from_request(request),
            )
        except DjangoValidationError as exc:
            detail = getattr(exc, "message_dict", None) or {"detail": exc.messages}
            raise ValidationError(detail) from exc
        proposal.refresh_from_db()
        return Response(self.get_serializer(proposal).data)

    @action(detail=False, methods=["post"])
    def scan(self, request):
        from apps.curator.services import CuratorService

        workspace = get_object_or_404(Workspace, pk=request.data.get("workspace"))
        if not PermissionService.check(
            request.user,
            workspace.resource,
            Permission.WRITE,
            api_key=api_key_from_request(request),
        ):
            raise PermissionDenied("Write permission required on the workspace.")
        return Response(CuratorService.scan_workspace(workspace))

    @action(detail=True, methods=["post"])
    def approve(self, request, pk=None):
        return self._decide(request, approve=True)

    @action(detail=True, methods=["post"])
    def reject(self, request, pk=None):
        return self._decide(request, approve=False)


class DeadlineViewSet(CreatePermissionMixin, PermissionFilterMixin, viewsets.ModelViewSet):
    """Deadlines extracted from the knowledge files (frontmatter + inline text)."""

    permission_classes = [IsAuthenticated, ResourcePermission]
    serializer_class = s.DeadlineSerializer
    ordering_fields = ["due_date", "created_at"]
    ordering = ["due_date"]

    def get_queryset(self):
        queryset = KnowledgeDeadline.objects.select_related(
            "document", "workspace", "project", "resource"
        )
        params = self.request.query_params
        if params.get("workspace"):
            queryset = queryset.filter(workspace_id=params["workspace"])
        if params.get("project"):
            queryset = queryset.filter(project_id=params["project"])
        if params.get("status"):
            queryset = queryset.filter(status__in=params["status"].split(","))
        if params.get("document"):
            queryset = queryset.filter(document_id=params["document"])
        if params.get("from"):
            queryset = queryset.filter(due_date__gte=params["from"])
        if params.get("to"):
            queryset = queryset.filter(due_date__lte=params["to"])
        return queryset

    def get_create_target(self, validated_data):
        document = validated_data.get("document")
        return document.resource if document is not None else None

    def create(self, request, *args, **kwargs):
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        document = data["document"]
        if not PermissionService.check(
            request.user, document.resource, Permission.WRITE, api_key=api_key_from_request(request)
        ):
            raise PermissionDenied("Write access required on the document.")
        deadline = KnowledgeDeadline.objects.create(
            document=document,
            resource=document.resource,
            workspace=document.workspace,
            project=document.project,
            title=data["title"],
            due_date=data["due_date"],
            status=data.get("status", "open"),
            source="manual",
            confidence=100,
        )
        return Response(s.DeadlineSerializer(deadline).data, status=status.HTTP_201_CREATED)


# ---------------------------------------------------------------------------
# Search
# ---------------------------------------------------------------------------
class FolderViewSet(CreatePermissionMixin, PermissionFilterMixin, viewsets.ModelViewSet):
    """Folders are permission-managed objects, so they are not superuser-only.

    Since ``documents/0004`` a folder carries its own ``Resource``, chained to
    the enclosing folder, which is what the engine walks. So access is decided
    on ``folder.resource``: read to see it, write to create/rename/move it,
    delete to remove it - the same rule every other resource-backed viewset
    uses, and the same rule the web UI applies.
    """

    permission_classes = [IsAuthenticated, ResourcePermission]
    serializer_class = s.FolderSerializer
    resource_field = "resource_id"
    search_fields = ["path"]
    ordering_fields = ["path", "created_at"]
    create_target_fields = ("project", "workspace")

    def get_queryset(self):
        return DocumentFolder.objects.select_related(
            "resource", "workspace", "project"
        )

    def get_create_target(self, validated_data):
        project = validated_data.get("project")
        if project is not None:
            return project.resource
        workspace = validated_data.get("workspace")
        return workspace.resource if workspace else None

    def perform_create(self, serializer):
        from apps.documents.folders import create_folder

        data = serializer.validated_data
        try:
            create_folder(
                workspace=data["workspace"],
                project=data.get("project"),
                path=data["path"],
                created_by=self.request.user,
            )
        except DjangoValidationError as exc:
            raise ValidationError(exc.message_dict) from exc
        serializer.instance = DocumentFolder.objects.get(
            workspace=data["workspace"], project=data.get("project"), path=serializer.validated_data["path"]
        )

    def create(self, request, *args, **kwargs):
        """Create a folder, optionally addressed by a ``node`` tree path.

        The additive node form: send ``workspace`` + ``node`` (the whole
        workspace-relative path) instead of ``project`` + ``path``, and the pair
        the storage layer needs is derived here.
        """
        node = (request.data.get("node") or "").strip("/")
        if not node:
            return super().create(request, *args, **kwargs)

        from apps.documents.folders import split_tree_path

        workspace = get_object_or_404(Workspace, pk=request.data.get("workspace"))
        project, scope_path = split_tree_path(workspace, node)
        data = request.data.copy()
        data["path"] = scope_path
        if project is not None:
            data["project"] = str(project.pk)
        serializer = self.get_serializer(data=data)
        serializer.is_valid(raise_exception=True)
        self.perform_create(serializer)
        headers = self.get_success_headers(serializer.data)
        return Response(serializer.data, status=status.HTTP_201_CREATED, headers=headers)

    def partial_update(self, request, *args, **kwargs):
        from apps.documents.folders import (
            folder_by_tree_path,
            move_folder,
            project_of_node,
            rename_folder,
        )

        folder = self.get_object()
        # The folder's own resource, not the project/workspace it lives in: a
        # grant on A/B/C must not be enough to move it, and access to the
        # workspace must not be what makes A/B/C writable.
        self._require_write(folder, request)

        node = (request.data.get("node") or "").strip("/")
        if node:
            # Additive node addressing for a move: a workspace-relative tree path.
            destination = folder_by_tree_path(folder.workspace, node)
            if destination is None:
                raise ValidationError({"node": f"Nincs ilyen csomópont: {node}"})
            dest_project = project_of_node(destination)
            if (dest_project.pk if dest_project else None) != folder.project_id:
                raise ValidationError({"node": "Csak a saját projektjén belül mozgatható."})
            try:
                move_folder(folder=folder, new_parent=destination, user=request.user)
            except DjangoValidationError as exc:
                raise ValidationError(exc.message_dict) from exc
            folder.refresh_from_db()
            return Response(self.get_serializer(folder).data)

        new_path = (request.data.get("path") or "").strip()
        try:
            if new_path and new_path != folder.path and "/" not in new_path:
                rename_folder(folder=folder, path=new_path)
            elif new_path and new_path.startswith(folder.path + "/"):
                rename_folder(folder=folder, path=new_path)
            elif new_path and new_path != folder.path:
                # Re-parent: strip the old name, append the new one.
                parent, _sep, _name = new_path.rpartition("/")
                move_folder(folder=folder, new_parent=parent, user=request.user)
        except DjangoValidationError as exc:
            raise ValidationError(exc.message_dict) from exc
        folder.refresh_from_db()
        return Response(self.get_serializer(folder).data)

    def destroy(self, request, *args, **kwargs):
        from django.core.exceptions import ValidationError

        from apps.documents.folders import delete_folder

        folder = self.get_object()
        # DELETE is gated by PermissionFilterMixin/ResourcePermission on the
        # folder's own resource.
        try:
            delete_folder(folder=folder, move_to_root=request.data.get("move") == "up")
        except ValidationError as exc:
            raise ValidationError(exc.message_dict) from exc
        return Response(status=204)


class RuntimeSettingViewSet(viewsets.ViewSet):
    """Read/override runtime settings (superuser only)."""

    permission_classes = [IsAuthenticated]
    lookup_field = "key"

    def _require_superuser(self, request):
        if not request.user.is_superuser:
            raise PermissionDenied("Superuser access required.")

    def list(self, request):
        self._require_superuser(request)
        return Response(describe())

    def retrieve(self, request, key=None):
        self._require_superuser(request)
        rows = {row["key"]: row for row in describe()}
        if key not in rows:
            raise ValidationError({"key": "Unknown setting."})
        return Response(rows[key])

    def partial_update(self, request, key=None):
        from django.core.exceptions import ValidationError as DjangoValidationError

        from apps.settings_store.services import clear_override, set_value

        self._require_superuser(request)
        if key not in {row["key"] for row in describe()}:
            raise ValidationError({"key": "Unknown setting."})
        if request.data.get("reset"):
            clear_override(key)
        else:
            try:
                set_value(key=key, raw=request.data.get("value", ""), user=request.user)
            except DjangoValidationError as exc:
                raise ValidationError(exc.message_dict) from exc
        rows = {row["key"]: row for row in describe()}
        return Response(rows[key])


class SearchView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        params = request.query_params
        query = params.get("q", "")
        mode = params.get("mode", "hybrid")
        try:
            limit = max(1, min(int(params.get("limit", 10)), 50))
        except (TypeError, ValueError):
            limit = 10

        api_key = api_key_from_request(request)
        results = SearchService.search(
            request.user,
            query,
            mode=mode,
            workspace_id=params.get("workspace"),
            project_id=params.get("project"),
            status=params.get("status"),
            limit=limit,
            api_key=api_key,
        )
        AuditService.log(
            AuditAction.SEARCH,
            user=request.user,
            api_key=api_key,
            source=AuditSource.API,
            request=request,
            detail={"q": query, "mode": mode, "count": len(results)},
        )
        return Response({"query": query, "mode": mode, "count": len(results), "results": results})


class LLMGuideView(APIView):
    """`GET /llm` -- self-describing manifest for an LLM agent.

    Public on purpose: an agent must be able to read how to obtain a key before
    it has one. Values that depend on the caller (current_caller, current
    settings) are only filled in when the request is authenticated.
    """

    permission_classes = [AllowAny]

    def get(self, request):
        return Response(build_manifest(request))


class DiscoveryView(APIView):
    """Agent-facing skill/pattern/convention/decision/example discovery."""

    permission_classes = [IsAuthenticated]

    def get(self, request):
        buckets = DiscoveryService.discover(
            request.user,
            api_key=api_key_from_request(request),
            types=request.query_params.getlist("type") or None,
            workspace_id=request.query_params.get("workspace"),
            limit=25,
        )
        return Response(buckets)


class QualityView(APIView):
    # Superuser-only, and this must stay in step with the MCP
    # ``knowledge_quality_metrics`` tool (which lives in apps.mcp): one gate or
    # the other is a way around the other.
    permission_classes = [IsAdminUser]

    def get(self, request):
        return Response(QualityService.metrics())


class DraftCreateView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request):
        workspace = get_object_or_404(Workspace, pk=request.data.get("workspace"))
        project = (
            get_object_or_404(Project, pk=request.data["project"])
            if request.data.get("project")
            else None
        )
        target = project.resource if project else workspace.resource
        if not PermissionService.check(
            request.user, target, Permission.WRITE, api_key=api_key_from_request(request)
        ):
            raise PermissionDenied("Write permission required.")
        prompt = request.data.get("prompt")
        if not prompt:
            raise ValidationError({"prompt": "This field is required."})
        document = DraftService.generate_draft(
            workspace=workspace,
            project=project,
            title=request.data.get("title") or "Untitled draft",
            prompt=prompt,
            user=request.user,
            request=request,
        )
        return Response(
            s.DocumentSerializer(document, context={"request": request}).data,
            status=status.HTTP_201_CREATED,
        )


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

    def _redact(self, rows):
        """Blank the payload of events the caller has no access to.

        Scoping the queryset to the caller's own events is not enough once the
        caller is a superuser: they see every event, and an event's ``detail`` is
        free text written by whichever service produced it - document titles,
        workspace names, secret labels - plus the workspace/project the event
        belongs to. A superuser has no implicit content access in Brainbox, so
        without this the admin trail is a directory of every document and
        workspace in the instance. Own events stay whole; someone else's are
        only readable if the caller can READ the event's resource.
        """
        if not rows:
            return rows
        caller_id = str(self.request.user.pk)
        candidates = {
            row["resource"]
            for row in rows
            if row.get("resource") and str(row.get("user") or "") != caller_id
        }
        readable = {
            str(resource_id)
            for resource_id in PermissionService.allowed_resource_ids(
                self.request.user,
                candidates,
                Permission.READ,
                api_key=api_key_from_request(self.request),
            )
        }
        for row in rows:
            if str(row.get("user") or "") == caller_id:
                continue
            if row.get("resource") and str(row["resource"]) in readable:
                continue
            row["detail"] = {}
            row["workspace"] = None
            row["project"] = None
        return rows

    def list(self, request, *args, **kwargs):
        response = super().list(request, *args, **kwargs)
        if isinstance(response.data, dict):
            self._redact(response.data.get("results"))
        return response

    def retrieve(self, request, *args, **kwargs):
        response = super().retrieve(request, *args, **kwargs)
        if isinstance(response.data, dict):
            self._redact([response.data])
        return response
