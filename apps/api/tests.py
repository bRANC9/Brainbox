import os
import subprocess
import tempfile
import uuid
from pathlib import Path

from django.test import SimpleTestCase, override_settings
from rest_framework import status, viewsets
from rest_framework.test import APITestCase

from apps.accounts.models import User
from apps.accounts.services import ApiKeyService
from apps.documents.services import DocumentService
from apps.git.services import GitService
from apps.groups.models import Group, GroupMembership
from apps.permissions.constants import Effect, Permission
from apps.resources.models import Resource
from apps.workspaces.services import ProjectService, WorkspaceService


@override_settings(KNOWLEDGE_DATA_ROOT=tempfile.mkdtemp())
class ApiTests(APITestCase):
    def setUp(self):
        self.alice = User.objects.create_user("alice", "alice@example.com", "pw")
        self.bob = User.objects.create_user("bob", "bob@example.com", "pw")
        self.company = WorkspaceService.create(name="Company", created_by=self.alice)

    def test_workspace_list_only_returns_visible(self):
        self.client.force_authenticate(self.alice)
        response = self.client.get("/api/v1/workspaces/")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["count"], 1)

        self.client.force_authenticate(self.bob)
        response = self.client.get("/api/v1/workspaces/")
        self.assertEqual(response.data["count"], 0)

    def test_create_document_and_read_content(self):
        project = ProjectService.create(workspace=self.company, name="Azure", created_by=self.alice)
        self.client.force_authenticate(self.alice)

        response = self.client.post(
            "/api/v1/documents/",
            {
                "workspace": str(self.company.pk),
                "project": str(project.pk),
                "title": "Deployment",
                "path": "azure/deployment.md",
                "content": "# Deployment\n\nHello",
                "status": "approved",
            },
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        document_id = response.data["id"]

        detail = self.client.get(f"/api/v1/documents/{document_id}/")
        self.assertEqual(detail.status_code, status.HTTP_200_OK)
        self.assertIn("Hello", detail.data["content"])

    def test_cannot_create_document_without_write_access(self):
        self.client.force_authenticate(self.bob)
        response = self.client.post(
            "/api/v1/documents/",
            {"workspace": str(self.company.pk), "title": "Nope", "content": "x"},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN, response.data)

    def test_api_key_scope_narrows_access(self):
        personal = WorkspaceService.create(name="Personal", created_by=self.alice)
        DocumentService.create(
            workspace=personal, title="Diary", content="secret", path="diary.md"
        )

        _, raw_key = ApiKeyService.create(
            user=self.alice,
            name="Claude - Company",
            scopes=[
                {"workspace": self.company, "permission": Permission.READ, "effect": Effect.ALLOW},
                {"workspace": personal, "permission": Permission.READ, "effect": Effect.DENY},
            ],
        )

        self.client.credentials(HTTP_AUTHORIZATION=f"ApiKey {raw_key}")
        response = self.client.get("/api/v1/workspaces/")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        names = {row["name"] for row in response.data["results"]}
        self.assertEqual(names, {"Company"})

        documents = self.client.get("/api/v1/documents/")
        titles = {row["title"] for row in documents.data["results"]}
        self.assertNotIn("Diary", titles)

    def test_invalid_api_key_is_rejected(self):
        self.client.credentials(HTTP_X_API_KEY="ck_live_deadbeefdeadbeefdeadbeef")
        response = self.client.get("/api/v1/workspaces/")
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)


@override_settings(KNOWLEDGE_DATA_ROOT=tempfile.mkdtemp())
class ResourceIdentifierTests(APITestCase):
    """``resource`` must always serialize as the Resource UUID.

    The field is a ``OneToOneField(primary_key=True)``, so a bare
    ``UUIDField()`` receives the *model instance* and renders it through
    ``Resource.__str__`` ("workspace:Acme") instead of the id. The value then
    cannot be fed back into any other endpoint.
    """

    def setUp(self):
        self.alice = User.objects.create_user("alice", "alice@example.com", "pw")
        self.workspace = WorkspaceService.create(name="Company", created_by=self.alice)
        self.project = ProjectService.create(
            workspace=self.workspace, name="Azure", created_by=self.alice
        )
        remote = Path(tempfile.mkdtemp()) / "vault"
        remote.mkdir()
        env = {**os.environ, "GIT_TERMINAL_PROMPT": "0"}
        subprocess.run(
            ["git", "init", "--quiet", "--initial-branch=main", str(remote)],
            check=True,
            capture_output=True,
            env=env,
        )
        (remote / "README.md").write_text("vault\n")
        # The remote has a worktree checked out; the service pushes into it.
        subprocess.run(
            ["git", "-C", str(remote), "config", "receive.denyCurrentBranch", "ignore"],
            check=True,
            env=env,
        )
        subprocess.run(["git", "-C", str(remote), "add", "-A"], check=True, env=env)
        subprocess.run(
            [
                "git", "-C", str(remote),
                "-c", "user.name=Test",
                "-c", "user.email=test@example.com",
                "commit", "--quiet", "-m", "init",
            ],
            check=True,
            capture_output=True,
            env=env,
        )
        self.repository = GitService.attach_repository(
            workspace=self.workspace,
            name="vault",
            remote_url=str(remote),
            created_by=self.alice,
        )
        self.document = DocumentService.create(
            workspace=self.workspace, title="Runbook", content="x", path="runbook.md"
        )
        self.client.force_authenticate(self.alice)

    def _assert_uuid_resource(self, payload):
        resource_id = payload["resource"]
        # Must round-trip as a real UUID. Serializers differ here: the explicit
        # UUIDField yields a string, an auto-generated PrimaryKeyRelatedField
        # yields a UUID object.
        self.assertEqual(str(uuid.UUID(str(resource_id))), str(resource_id).lower())
        # ...and identify the same object as the primary key.
        self.assertEqual(str(resource_id).lower(), str(payload["id"]).lower())

    def test_workspace_resource_is_uuid(self):
        response = self.client.get(f"/api/v1/workspaces/{self.workspace.pk}/")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self._assert_uuid_resource(response.data)

    def test_project_resource_is_uuid(self):
        response = self.client.get(f"/api/v1/projects/{self.project.pk}/")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self._assert_uuid_resource(response.data)

    def test_git_repository_resource_is_uuid(self):
        response = self.client.get(f"/api/v1/git/{self.repository.pk}/")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self._assert_uuid_resource(response.data)

    def test_document_resource_is_uuid(self):
        response = self.client.get(f"/api/v1/documents/{self.document.pk}/")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self._assert_uuid_resource(response.data)

    def test_resource_value_is_usable_as_acl_target(self):
        """The id handed out by the API must resolve against a Resource."""
        listing = self.client.get("/api/v1/workspaces/")
        resource_id = listing.data["results"][0]["resource"]
        self.assertTrue(
            Resource.objects.filter(pk=resource_id).exists(),
            f"{resource_id!r} is not a valid Resource id",
        )


@override_settings(KNOWLEDGE_DATA_ROOT=tempfile.mkdtemp())
class GroupMembersTests(APITestCase):
    """``GET``/``POST`` share one ``/members/`` path.

    Two ``@action``s with the same explicit ``url_path`` used to collide: Django
    resolves the first registered pattern, so the POST-only view answered every
    verb and GET came back 405.
    """

    def setUp(self):
        self.alice = User.objects.create_user("alice", "alice@example.com", "pw")
        self.bob = User.objects.create_user("bob", "bob@example.com", "pw")
        self.admin = User.objects.create_user(
            "root", "root@example.com", "pw", is_staff=True, is_superuser=True
        )
        self.group = Group.objects.create(name="Platform", created_by=self.admin)
        self.client.force_authenticate(self.admin)

    def test_list_members_is_readable(self):
        GroupMembership.objects.create(user=self.alice, group=self.group)
        response = self.client.get(f"/api/v1/groups/{self.group.pk}/members/")
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertEqual(len(response.data), 1)
        self.assertEqual(str(response.data[0]["user"]), str(self.alice.pk))

    def test_add_member_still_works(self):
        response = self.client.post(
            f"/api/v1/groups/{self.group.pk}/members/",
            {"user": str(self.bob.pk), "role": "member"},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        self.assertTrue(
            GroupMembership.objects.filter(user=self.bob, group=self.group).exists()
        )

    def test_non_admin_cannot_add_member(self):
        self.client.force_authenticate(self.alice)
        response = self.client.post(
            f"/api/v1/groups/{self.group.pk}/members/",
            {"user": str(self.bob.pk)},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)


class ActionRouteTests(SimpleTestCase):
    """No two ``@action``s on a viewset may resolve to the same URL path."""

    def test_actions_have_unique_paths(self):
        for viewset in viewsets.__dict__.values():
            actions = getattr(viewset, "actions", None)
            if not isinstance(actions, dict) or not actions:
                continue
            for method, routes in actions.items():
                paths = [route["url_path"] for route in routes]
                duplicates = {p for p in paths if paths.count(p) > 1}
                self.assertEqual(
                    duplicates, set(), f"{viewset.__name__}.{method} has duplicate paths"
                )
