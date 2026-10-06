import json
import os
import subprocess
import tempfile
import uuid
from pathlib import Path
from unittest.mock import patch

from django.test import SimpleTestCase, override_settings
from rest_framework import status, viewsets
from rest_framework.test import APITestCase

from apps.accounts.models import ApiKey, User
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

    def test_api_key_cannot_mint_another_api_key(self):
        """A leaked agent key must not be able to bootstrap a permanent one."""
        _api_key, raw_key = ApiKeyService.create(user=self.alice, name="agent")
        response = self.client.post(
            "/api/v1/api-keys/", {"name": "escalated"}, format="json", HTTP_X_API_KEY=raw_key
        )
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN, response.data)
        self.assertEqual(ApiKey.objects.filter(user=self.alice).count(), 1)

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


class LLMManifestTests(APITestCase):
    """`GET /llm` is the agent's own docs -- it must stay truthful and leak nothing."""

    def manifest(self, **extra):
        return self.client.get("/llm", **extra).json()

    def test_manifest_is_public(self):
        self.assertIsNone(self.manifest()["current_caller"])

    def test_manifest_documents_api_key_auth(self):
        auth = self.manifest()["authentication"]
        self.assertEqual(auth["header"], "Authorization: ApiKey <API_KEY>")
        self.assertEqual(auth["alternative_header"], "X-API-Key: <API_KEY>")

    def test_manifest_lists_every_registered_collection_and_mcp_tool(self):
        from apps.api.urls import router
        from apps.mcp.tools import definitions

        data = self.manifest()
        paths = {row["path"] for row in data["transports"]["rest"]["endpoints"]}
        for prefix, _viewset, _basename in router.registry:
            self.assertIn(f"/api/v1/{prefix}/", paths)
        self.assertIn("/api/v1/search/", paths)
        self.assertIn("/api/v1/llm/", paths)

        tools = {row["name"] for row in data["transports"]["mcp"]["tools"]}
        self.assertEqual(tools, {row["name"] for row in definitions()})

    def test_manifest_reports_the_caller_but_never_the_key(self):
        alice = User.objects.create_user("alice", "alice@example.com", "pw")
        _api_key, raw_key = ApiKeyService.create(user=alice, name="agent")

        data = self.manifest(HTTP_X_API_KEY=raw_key)
        self.assertEqual(data["current_caller"]["username"], "alice")
        self.assertEqual(data["current_caller"]["api_key"]["name"], "agent")
        self.assertNotIn(raw_key, json.dumps(data, default=str))

    def test_setting_values_are_superuser_only(self):
        runtime = self.manifest()["runtime_settings"]
        self.assertFalse(runtime["values_visible"])
        for row in runtime["catalog"]:
            self.assertNotIn("current", row)

        admin = User.objects.create_user(
            "root", "root@example.com", "pw", is_staff=True, is_superuser=True
        )
        self.client.force_login(admin)
        self.assertTrue(self.manifest()["runtime_settings"]["values_visible"])

    def test_endpoint_table_is_clean(self):
        """No regex routes, no format-suffix clones, no methodless rows."""
        seen: set[str] = set()
        rows = self.manifest()["transports"]["rest"]["endpoints"]
        self.assertTrue(rows)
        for row in rows:
            path = row["path"]
            self.assertTrue(path.startswith("/api/v1/"), path)
            for leaked in ("^", "$", "\\", "(?", "format"):
                self.assertNotIn(leaked, path, f"{path} leaks a raw route pattern")
            self.assertTrue(row["methods"], path)
            self.assertNotIn(path, seen, f"{path} listed twice")
            seen.add(path)

    def test_browsers_get_the_page_agents_get_json(self):
        page = self.client.get("/llm", HTTP_ACCEPT="text/html,application/xhtml+xml")
        self.assertEqual(page.status_code, status.HTTP_200_OK)
        self.assertIn("text/html", page["Content-Type"])
        self.assertIn("Authorization: ApiKey", page.content.decode())
        # No browser Accept header (curl, SDKs) must stay machine readable.
        self.assertEqual(self.client.get("/llm")["Content-Type"], "application/json")
        self.assertEqual(
            self.client.get("/llm", HTTP_ACCEPT="*/*")["Content-Type"], "application/json"
        )
        self.assertEqual(
            self.client.get("/llm", HTTP_ACCEPT="application/json")["Content-Type"],
            "application/json",
        )

    def test_json_contract_also_lives_under_the_api_namespace(self):
        response = self.client.get("/api/v1/llm/")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["authentication"]["header"], "Authorization: ApiKey <API_KEY>")

    def test_invalid_key_is_rejected_on_the_page(self):
        response = self.client.get("/llm", HTTP_X_API_KEY="definitely-not-a-key")
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_api_root_is_not_a_surface(self):
        """`/api/v1/` used to be DRF's browsable index; `/llm` replaced it."""
        self.assertEqual(
            self.client.get("/api/v1/").status_code, status.HTTP_404_NOT_FOUND
        )


@override_settings(KNOWLEDGE_DATA_ROOT=tempfile.mkdtemp())
class NodeAddressingTests(APITestCase):
    """The additive node address: ``tree_path`` out, ``node`` accepted in.

    The API keeps addressing by project + scope path; a node's workspace-relative
    tree path is exposed alongside, and accepted where a folder or document is
    placed, so the API speaks the same address the web pages use.
    """

    def setUp(self):
        self.alice = User.objects.create_user("alice", "alice@example.com", "pw")
        self.workspace = WorkspaceService.create(name="Ecoform", created_by=self.alice)
        self.project = ProjectService.create(
            workspace=self.workspace, name="Deploy", created_by=self.alice
        )
        self.client.force_authenticate(self.alice)

    def test_folder_response_carries_the_tree_path(self):
        response = self.client.post(
            "/api/v1/folders/",
            {
                "workspace": str(self.workspace.pk),
                "project": str(self.project.pk),
                "path": "runbooks",
            },
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        self.assertEqual(response.data["tree_path"], "Deploy/runbooks")

    def test_folder_create_by_node(self):
        response = self.client.post(
            "/api/v1/folders/",
            {"workspace": str(self.workspace.pk), "node": "Deploy/runbooks/2024"},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        self.assertEqual(response.data["tree_path"], "Deploy/runbooks/2024")
        self.assertEqual(response.data["path"], "runbooks/2024")

    def test_folder_move_by_node(self):
        from apps.documents.folders import create_folder
        from apps.documents.models import DocumentFolder

        create_folder(
            workspace=self.workspace, project=self.project, path="python/legacy",
            created_by=self.alice,
        )
        create_folder(
            workspace=self.workspace, project=self.project, path="target",
            created_by=self.alice,
        )
        folder = DocumentFolder.objects.get(name="legacy")
        response = self.client.patch(
            f"/api/v1/folders/{folder.pk}/", {"node": "Deploy/target"}, format="json"
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        folder.refresh_from_db()
        self.assertEqual(folder.path, "target/legacy")

    def test_document_move_by_node(self):
        from apps.documents.folders import create_folder

        create_folder(
            workspace=self.workspace, project=self.project, path="dotnet",
            created_by=self.alice,
        )
        document = DocumentService.create(
            workspace=self.workspace, project=self.project, title="D",
            path="x.md", content="# D\n", created_by=self.alice,
        )
        response = self.client.post(
            f"/api/v1/documents/{document.pk}/move/",
            {"node": "Deploy/dotnet"},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        document.refresh_from_db()
        self.assertEqual(document.path, "dotnet/x.md")
        self.assertEqual(response.data["tree_path"], "Deploy/dotnet/x.md")

    def test_document_move_by_node_rejects_cross_project(self):
        other = ProjectService.create(
            workspace=self.workspace, name="Other", created_by=self.alice
        )
        document = DocumentService.create(
            workspace=self.workspace, project=self.project, title="D",
            path="x.md", content="# D\n", created_by=self.alice,
        )
        response = self.client.post(
            f"/api/v1/documents/{document.pk}/move/",
            {"node": other.name},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        document.refresh_from_db()
        self.assertEqual(document.path, "x.md")


@override_settings(KNOWLEDGE_DATA_ROOT=tempfile.mkdtemp())
class GatewayApiTests(APITestCase):
    """The gateway target collection and its call action.

    Creating/list is ordinary resource CRUD; calling needs USE and runs through
    the service, whose transport is mocked so no real request leaves the test.
    """

    def setUp(self):
        self.alice = User.objects.create_user("alice", "alice@example.com", "pw")
        self.bob = User.objects.create_user("bob", "bob@example.com", "pw")
        self.workspace = WorkspaceService.create(name="Company", created_by=self.alice)
        self.client.force_authenticate(self.alice)

    def _create(self):
        return self.client.post(
            "/api/v1/gateway/",
            {
                "name": "GitHub",
                "base_url": "https://api.github.com",
                "kind": "http",
                "workspace": str(self.workspace.pk),
                "config": {"auth": "none"},
            },
            format="json",
        )

    def test_create_and_list(self):
        response = self._create()
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        self.assertEqual(response.data["kind"], "http")
        self.assertIsNone(response.data["project"])
        listing = self.client.get("/api/v1/gateway/")
        self.assertEqual(listing.status_code, status.HTTP_200_OK)
        self.assertEqual(listing.data["count"], 1)

    def test_call_returns_the_remote_response(self):
        target_id = self._create().data["id"]
        with patch(
            "apps.gateway.services.GatewayService._send",
            return_value=(200, {"Content-Type": "application/json"}, '{"ok": true}', False),
        ):
            response = self.client.post(
                f"/api/v1/gateway/{target_id}/call/",
                {"method": "GET", "path": "user"},
                format="json",
            )
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertEqual(response.data["status"], 200)
        self.assertIn('"ok"', response.data["body"])

    def test_another_user_cannot_call(self):
        target_id = self._create().data["id"]
        self.client.force_authenticate(self.bob)
        response = self.client.post(
            f"/api/v1/gateway/{target_id}/call/", {"method": "GET"}, format="json"
        )
        self.assertIn(
            response.status_code,
            (status.HTTP_403_FORBIDDEN, status.HTTP_404_NOT_FOUND),
        )

    def test_bad_config_is_a_400(self):
        response = self.client.post(
            "/api/v1/gateway/",
            {
                "name": "Bad",
                "base_url": "https://api.github.com",
                "workspace": str(self.workspace.pk),
                "config": {"auth": "query"},
            },
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("config", response.data)

    def test_rate_limit_is_a_429(self):
        created = self.client.post(
            "/api/v1/gateway/",
            {
                "name": "Limited",
                "base_url": "https://api.github.com",
                "workspace": str(self.workspace.pk),
                "config": {"rate_limit": 1, "rate_window_seconds": 60},
            },
            format="json",
        )
        target_id = created.data["id"]
        with patch(
            "apps.gateway.services.GatewayService._send",
            return_value=(200, {}, "{}", False),
        ):
            first = self.client.post(
                f"/api/v1/gateway/{target_id}/call/", {"method": "GET"}, format="json"
            )
            second = self.client.post(
                f"/api/v1/gateway/{target_id}/call/", {"method": "GET"}, format="json"
            )
        self.assertEqual(first.status_code, status.HTTP_200_OK)
        self.assertEqual(second.status_code, status.HTTP_429_TOO_MANY_REQUESTS)


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
