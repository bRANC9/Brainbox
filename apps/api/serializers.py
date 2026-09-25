"""DRF serializers. Domain writes are delegated to the application services."""

from __future__ import annotations

from django.core.exceptions import ValidationError as DjangoValidationError
from rest_framework import serializers
from rest_framework.exceptions import ValidationError as DRFValidationError

from apps.accounts.models import ApiKey, ApiKeyScope, User
from apps.audit.models import AuditEvent
from apps.documents.models import Document, DocumentVersion
from apps.documents.services import DocumentService
from apps.files.models import File, FileVersion
from apps.git.models import GitCommitReference, GitRepository, GitSyncState
from apps.groups.models import Group, GroupMembership
from apps.links.models import ResourceLink
from apps.permissions.constants import Effect, Permission
from apps.permissions.models import ResourceACL
from apps.resources.models import Resource
from apps.secrets.models import Secret, SecretAttachment
from apps.workspaces.models import Project, Workspace
from apps.workspaces.services import ProjectService, WorkspaceService


def _actor(serializer):
    request = serializer.context.get("request")
    user = getattr(request, "user", None)
    if user is not None and getattr(user, "is_authenticated", False):
        return user
    return None


def _rethrow(exc: DjangoValidationError):
    if hasattr(exc, "message_dict"):
        raise DRFValidationError(exc.message_dict) from exc
    raise DRFValidationError(exc.messages) from exc


# ---------------------------------------------------------------------------
# Accounts / groups
# ---------------------------------------------------------------------------
class UserSerializer(serializers.ModelSerializer):
    password = serializers.CharField(write_only=True, required=False, allow_blank=True)

    class Meta:
        model = User
        fields = [
            "id",
            "username",
            "email",
            "display_name",
            "first_name",
            "last_name",
            "is_active",
            "is_staff",
            "is_superuser",
            "password",
            "date_joined",
        ]
        read_only_fields = ["id", "date_joined"]

    def create(self, validated_data):
        password = validated_data.pop("password", None)
        user = User(**validated_data)
        if password:
            user.set_password(password)
        else:
            user.set_unusable_password()
        user.save()
        return user

    def update(self, instance, validated_data):
        password = validated_data.pop("password", None)
        for field, value in validated_data.items():
            setattr(instance, field, value)
        if password:
            instance.set_password(password)
        instance.save()
        return instance


class SelfUserSerializer(UserSerializer):
    class Meta(UserSerializer.Meta):
        read_only_fields = ["id", "is_active", "is_staff", "is_superuser", "date_joined"]


class GroupMembershipSerializer(serializers.ModelSerializer):
    class Meta:
        model = GroupMembership
        fields = ["id", "user", "group", "role", "created_at"]
        read_only_fields = ["id", "created_at"]


class GroupSerializer(serializers.ModelSerializer):
    member_count = serializers.IntegerField(source="memberships.count", read_only=True)

    class Meta:
        model = Group
        fields = ["id", "name", "description", "member_count", "created_at", "updated_at"]
        read_only_fields = ["id", "created_at", "updated_at"]


# ---------------------------------------------------------------------------
# Resources / ACL
# ---------------------------------------------------------------------------
class ResourceSerializer(serializers.ModelSerializer):
    class Meta:
        model = Resource
        fields = [
            "id",
            "resource_type",
            "name",
            "parent",
            "metadata",
            "created_by",
            "created_at",
            "updated_at",
        ]
        read_only_fields = ["id", "created_at", "updated_at"]


class ResourceACLSerializer(serializers.ModelSerializer):
    class Meta:
        model = ResourceACL
        fields = [
            "id",
            "resource",
            "subject_type",
            "subject_id",
            "permission",
            "effect",
            "inherit",
            "created_by",
            "created_at",
        ]
        read_only_fields = ["id", "created_by", "created_at"]


class ResourceLinkSerializer(serializers.ModelSerializer):
    target_name = serializers.CharField(source="target.name", read_only=True)

    class Meta:
        model = ResourceLink
        fields = ["id", "source", "target", "link_type", "target_name", "created_by", "created_at"]
        read_only_fields = ["id", "created_by", "created_at"]


# ---------------------------------------------------------------------------
# Workspaces / projects
# ---------------------------------------------------------------------------
class WorkspaceSerializer(serializers.ModelSerializer):
    id = serializers.UUIDField(source="pk", read_only=True)
    resource = serializers.UUIDField(read_only=True)
    document_count = serializers.IntegerField(source="documents.count", read_only=True)
    project_count = serializers.IntegerField(source="project_set.count", read_only=True)

    class Meta:
        model = Workspace
        fields = [
            "id",
            "resource",
            "name",
            "slug",
            "description",
            "document_count",
            "project_count",
            "created_by",
            "created_at",
            "updated_at",
        ]
        read_only_fields = [
            "id",
            "resource",
            "slug",
            "document_count",
            "project_count",
            "created_by",
            "created_at",
            "updated_at",
        ]

    def create(self, validated_data):
        return WorkspaceService.create(
            name=validated_data["name"],
            slug=validated_data.get("slug"),
            description=validated_data.get("description", ""),
            created_by=_actor(self),
            request=self.context.get("request"),
        )

    def update(self, instance, validated_data):
        instance.name = validated_data.get("name", instance.name)
        instance.description = validated_data.get("description", instance.description)
        instance.save()
        instance.resource.name = instance.name
        instance.resource.save(update_fields=["name", "updated_at"])
        return instance


class ProjectSerializer(serializers.ModelSerializer):
    id = serializers.UUIDField(source="pk", read_only=True)
    resource = serializers.UUIDField(read_only=True)
    workspace = serializers.PrimaryKeyRelatedField(queryset=Workspace.objects.all())
    document_count = serializers.IntegerField(source="documents.count", read_only=True)

    class Meta:
        model = Project
        fields = [
            "id",
            "resource",
            "workspace",
            "name",
            "slug",
            "description",
            "document_count",
            "created_by",
            "created_at",
            "updated_at",
        ]
        read_only_fields = [
            "id",
            "resource",
            "slug",
            "document_count",
            "created_by",
            "created_at",
            "updated_at",
        ]

    def create(self, validated_data):
        return ProjectService.create(
            workspace=validated_data["workspace"],
            name=validated_data["name"],
            slug=validated_data.get("slug"),
            description=validated_data.get("description", ""),
            created_by=_actor(self),
            request=self.context.get("request"),
        )

    def update(self, instance, validated_data):
        instance.name = validated_data.get("name", instance.name)
        instance.description = validated_data.get("description", instance.description)
        instance.save()
        instance.resource.name = instance.name
        instance.resource.save(update_fields=["name", "updated_at"])
        return instance


# ---------------------------------------------------------------------------
# Documents
# ---------------------------------------------------------------------------
class DocumentVersionSerializer(serializers.ModelSerializer):
    class Meta:
        model = DocumentVersion
        fields = [
            "id",
            "document",
            "version",
            "summary",
            "metadata",
            "change_type",
            "source",
            "created_by",
            "api_key",
            "git_commit",
            "created_at",
        ]
        read_only_fields = fields


class DocumentSerializer(serializers.ModelSerializer):
    id = serializers.UUIDField(source="pk", read_only=True)
    content = serializers.SerializerMethodField()
    workspace = serializers.PrimaryKeyRelatedField(queryset=Workspace.objects.all())
    project = serializers.PrimaryKeyRelatedField(
        queryset=Project.objects.all(), required=False, allow_null=True
    )

    class Meta:
        model = Document
        fields = [
            "id",
            "resource",
            "workspace",
            "project",
            "title",
            "slug",
            "path",
            "summary",
            "mime_type",
            "status",
            "priority",
            "frontmatter",
            "metadata",
            "current_version",
            "created_by",
            "created_at",
            "updated_at",
            "content",
        ]
        read_only_fields = [
            "resource",
            "slug",
            "frontmatter",
            "current_version",
            "created_by",
            "created_at",
            "updated_at",
        ]
        extra_kwargs = {
            "title": {"required": False, "allow_blank": True},
            "path": {"required": False, "allow_blank": True},
        }
        # Path uniqueness is conditional (project-scoped) and enforced by the
        # service + DB constraint; DRF's derived unique-together validator would
        # wrongly demand `path` on every create.
        validators: list = []

    def get_content(self, obj):
        if not self.context.get("include_content", True):
            return None
        try:
            return DocumentService.read_content(obj)
        except Exception:  # pragma: no cover - missing file on disk
            return ""

    def create(self, validated_data):
        try:
            return DocumentService.create(
                workspace=validated_data["workspace"],
                project=validated_data.get("project"),
                title=validated_data.get("title", ""),
                path=validated_data.get("path"),
                content=self.initial_data.get("content", ""),
                summary=validated_data.get("summary", ""),
                metadata=validated_data.get("metadata") or {},
                status=validated_data.get("status"),
                priority=validated_data.get("priority"),
                created_by=_actor(self),
                request=self.context.get("request"),
            )
        except DjangoValidationError as exc:
            _rethrow(exc)

    def update(self, instance, validated_data):
        content = self.initial_data.get("content", None)
        if content is not None:
            return DocumentService.update_content(
                document=instance,
                content=content,
                title=validated_data.get("title"),
                summary=validated_data.get("summary"),
                metadata=validated_data.get("metadata"),
                status=validated_data.get("status"),
                priority=validated_data.get("priority"),
                user=_actor(self),
                request=self.context.get("request"),
            )
        for field in ("title", "summary", "status", "priority", "metadata", "project"):
            if field in validated_data:
                setattr(instance, field, validated_data[field])
        instance.save()
        return instance


# ---------------------------------------------------------------------------
# Files
# ---------------------------------------------------------------------------
class FileVersionSerializer(serializers.ModelSerializer):
    class Meta:
        model = FileVersion
        fields = [
            "id",
            "file",
            "version",
            "size",
            "checksum",
            "change_type",
            "source",
            "created_by",
            "created_at",
        ]
        read_only_fields = fields


class FileSerializer(serializers.ModelSerializer):
    id = serializers.UUIDField(source="pk", read_only=True)
    workspace = serializers.PrimaryKeyRelatedField(queryset=Workspace.objects.all())
    project = serializers.PrimaryKeyRelatedField(
        queryset=Project.objects.all(), required=False, allow_null=True
    )

    class Meta:
        model = File
        fields = [
            "id",
            "resource",
            "workspace",
            "project",
            "name",
            "path",
            "mime_type",
            "size",
            "checksum",
            "metadata",
            "current_version",
            "created_by",
            "created_at",
            "updated_at",
        ]
        read_only_fields = [
            "resource",
            "mime_type",
            "size",
            "checksum",
            "current_version",
            "created_by",
            "created_at",
            "updated_at",
        ]
        validators: list = []


# ---------------------------------------------------------------------------
# API keys
# ---------------------------------------------------------------------------
class ApiKeyScopeSerializer(serializers.ModelSerializer):
    class Meta:
        model = ApiKeyScope
        fields = ["id", "workspace", "project", "permission", "effect", "created_at"]
        read_only_fields = ["id", "created_at"]


class ApiKeyScopeInputSerializer(serializers.Serializer):
    workspace = serializers.PrimaryKeyRelatedField(
        queryset=Workspace.objects.all(), required=False, allow_null=True
    )
    project = serializers.PrimaryKeyRelatedField(
        queryset=Project.objects.all(), required=False, allow_null=True
    )
    permission = serializers.ChoiceField(choices=Permission.choices)
    effect = serializers.ChoiceField(choices=Effect.choices, default=Effect.ALLOW)


class ApiKeySerializer(serializers.ModelSerializer):
    scopes = ApiKeyScopeSerializer(many=True, read_only=True)

    class Meta:
        model = ApiKey
        fields = [
            "id",
            "name",
            "key_prefix",
            "is_active",
            "expires_at",
            "last_used_at",
            "revoked_at",
            "created_at",
            "scopes",
        ]
        read_only_fields = fields


class ApiKeyCreateSerializer(serializers.Serializer):
    name = serializers.CharField(max_length=255)
    expires_at = serializers.DateTimeField(required=False, allow_null=True)
    scopes = ApiKeyScopeInputSerializer(many=True, required=False)


# ---------------------------------------------------------------------------
# Git
# ---------------------------------------------------------------------------
class GitSyncStateSerializer(serializers.ModelSerializer):
    class Meta:
        model = GitSyncState
        fields = [
            "status",
            "branch",
            "last_synced_sha",
            "last_pulled_at",
            "last_pushed_at",
            "last_error",
            "updated_at",
        ]
        read_only_fields = fields


class GitCommitReferenceSerializer(serializers.ModelSerializer):
    class Meta:
        model = GitCommitReference
        fields = [
            "id",
            "sha",
            "branch",
            "message",
            "author_name",
            "author_email",
            "direction",
            "created_by",
            "committed_at",
            "created_at",
        ]
        read_only_fields = fields


class GitRepositorySerializer(serializers.ModelSerializer):
    id = serializers.UUIDField(source="pk", read_only=True)
    resource = serializers.UUIDField(read_only=True)
    workspace = serializers.PrimaryKeyRelatedField(queryset=Workspace.objects.all())
    project = serializers.PrimaryKeyRelatedField(
        queryset=Project.objects.all(), required=False, allow_null=True
    )
    scope_label = serializers.CharField(read_only=True)
    sync_state = GitSyncStateSerializer(read_only=True)
    commits = GitCommitReferenceSerializer(many=True, read_only=True)

    class Meta:
        model = GitRepository
        fields = [
            "id",
            "resource",
            "workspace",
            "project",
            "name",
            "remote_url",
            "default_branch",
            "workflow",
            "auto_sync",
            "is_active",
            "scope_label",
            "sync_state",
            "commits",
            "created_by",
            "created_at",
            "updated_at",
        ]
        read_only_fields = [
            "id",
            "resource",
            "scope_label",
            "sync_state",
            "commits",
            "created_by",
            "created_at",
            "updated_at",
        ]
        extra_kwargs = {"name": {"required": False, "allow_blank": True}}
        validators: list = []

    def create(self, validated_data):
        from apps.git.services import GitService

        return GitService.attach_repository(
            workspace=validated_data["workspace"],
            project=validated_data.get("project"),
            remote_url=validated_data.get("remote_url", ""),
            name=validated_data.get("name", ""),
            default_branch=validated_data.get("default_branch", "main"),
            workflow=validated_data.get("workflow", "direct_commit"),
            created_by=_actor(self),
            request=self.context.get("request"),
        )

    def update(self, instance, validated_data):
        for field in ("name", "remote_url", "default_branch", "workflow", "auto_sync", "is_active"):
            if field in validated_data:
                setattr(instance, field, validated_data[field])
        instance.save()
        return instance


# ---------------------------------------------------------------------------
# Secrets
# ---------------------------------------------------------------------------
class SecretAttachmentSerializer(serializers.ModelSerializer):
    class Meta:
        model = SecretAttachment
        fields = ["id", "workspace", "project", "created_at"]
        read_only_fields = ["id", "created_at"]


class SecretSerializer(serializers.ModelSerializer):
    id = serializers.UUIDField(read_only=True)
    attachments = SecretAttachmentSerializer(many=True, read_only=True)

    class Meta:
        model = Secret
        fields = [
            "id",
            "name",
            "description",
            "secret_type",
            "metadata",
            "is_active",
            "fingerprint",
            "last_used_at",
            "created_at",
            "updated_at",
            "attachments",
        ]
        read_only_fields = fields


class SecretCreateSerializer(serializers.ModelSerializer):
    payload = serializers.JSONField(write_only=True)

    class Meta:
        model = Secret
        fields = ["name", "description", "secret_type", "metadata", "payload"]
        extra_kwargs = {
            "description": {"required": False, "allow_blank": True},
            "metadata": {"required": False},
            "secret_type": {"required": False},
        }
        validators: list = []


# ---------------------------------------------------------------------------
# Audit
# ---------------------------------------------------------------------------
class AuditEventSerializer(serializers.ModelSerializer):
    class Meta:
        model = AuditEvent
        fields = [
            "id",
            "timestamp",
            "user",
            "api_key",
            "action",
            "resource",
            "workspace",
            "project",
            "source",
            "ip_address",
            "user_agent",
            "result",
            "version",
            "git_commit",
            "detail",
        ]
        read_only_fields = fields
