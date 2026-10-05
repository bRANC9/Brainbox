import uuid

from django.conf import settings
from django.db import models


class ResourceType(models.TextChoices):
    WORKSPACE = "workspace", "Workspace"
    PROJECT = "project", "Project"
    FOLDER = "folder", "Folder"
    DOCUMENT = "document", "Document"
    FILE = "file", "File"
    GIT_REPOSITORY = "git_repository", "Git repository"
    SECRET = "secret", "Secret"
    MCP_SERVER = "mcp_server", "MCP server"
    GATEWAY = "gateway", "Gateway target"
    API_KEY = "api_key", "API key"


class Resource(models.Model):
    """Universal identity every permission-managed object hangs off.

    Permissions are always evaluated on Resources; domain objects (Workspace,
    Project, Document, File, ...) attach one-to-one. `parent` forms the
    inheritance chain used by the permission engine.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    resource_type = models.CharField(max_length=32, choices=ResourceType.choices, db_index=True)
    name = models.CharField(max_length=255)
    parent = models.ForeignKey(
        "self",
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="children",
    )
    metadata = models.JSONField(default=dict, blank=True)
    no_takeover = models.BooleanField(
        default=False,
        help_text="Zárja ki a superuser 'jogosultság átvétel' útját. Öröklődik: "
        "ha egy workspace-en be van kapcsolva, az egész fán nem lehet "
        "ADMIN-t szerezni. Csak a workspace tulajdonosa kapcsolhatja be.",
    )
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="created_resources",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "resources_resource"
        ordering = ["resource_type", "name"]
        indexes = [models.Index(fields=["resource_type", "name"])]

    def __str__(self) -> str:
        return f"{self.resource_type}:{self.name}"

    # -- hierarchy -----------------------------------------------------------
    def ancestors(self):
        """Yield this resource, then each parent up to the root."""
        node = self
        seen: set[uuid.UUID] = set()
        while node is not None and node.id not in seen:
            seen.add(node.id)
            yield node
            node = node.parent

    def ancestor_of_type(self, resource_type: str):
        for resource in self.ancestors():
            if resource.resource_type == resource_type:
                return resource
        return None

    def workspace_resource(self):
        return self.ancestor_of_type(ResourceType.WORKSPACE)

    def project_resource(self):
        return self.ancestor_of_type(ResourceType.PROJECT)

    def folder_resource(self):
        """The innermost folder on the chain, if the resource lives in one."""
        return self.ancestor_of_type(ResourceType.FOLDER)

    def takeover_locked(self) -> bool:
        """True when this resource or any ancestor forbids the takeover path."""
        return any(node.no_takeover for node in self.ancestors())
