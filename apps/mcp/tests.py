import json
import tempfile
from unittest.mock import patch

from django.test import TestCase, override_settings

from apps.accounts.models import User
from apps.accounts.services import ApiKeyService
from apps.audit.models import AuditAction, AuditEvent
from apps.documents.models import DocumentStatus
from apps.documents.services import DocumentService
from apps.links.services import LinkService
from apps.permissions.constants import Permission, SubjectType
from apps.permissions.services import PermissionService
from apps.workspaces.personal import PersonalWorkspaceService
from apps.workspaces.services import ProjectService, WorkspaceService


@override_settings(KNOWLEDGE_DATA_ROOT=tempfile.mkdtemp())
class MCPTests(TestCase):
    def setUp(self):
        self.alice = User.objects.create_user("alice", "alice@example.com", "pw")
        self.bob = User.objects.create_user("bob", "bob@example.com", "pw")
        self.workspace = WorkspaceService.create(name="Company", created_by=self.alice)
        self.skill = DocumentService.create(
            workspace=self.workspace,
            title="Bicep skill",
            path="skills/bicep.md",
            content="# Bicep skill\n\nUse modules for container apps.\n",
            status=DocumentStatus.APPROVED,
            created_by=self.alice,
        )
        _, self.alice_key = ApiKeyService.create(user=self.alice, name="alice")
        _, self.bob_key = ApiKeyService.create(user=self.bob, name="bob")

    def _call(self, payload, key):
        return self.client.post(
            "/mcp",
            data=json.dumps(payload),
            content_type="application/json",
            HTTP_AUTHORIZATION=f"ApiKey {key}",
        )

    def _tool(self, name, arguments, key):
        """Call a tool and return the raw MCP result (payload or isError)."""
        response = self._call(
            {
                "jsonrpc": "2.0",
                "id": 99,
                "method": "tools/call",
                "params": {"name": name, "arguments": arguments},
            },
            key,
        )
        return response.json()["result"]

    def _payload(self, name, arguments, key):
        result = self._tool(name, arguments, key)
        self.assertFalse(result.get("isError"), result)
        return json.loads(result["content"][0]["text"])

    def _assert_error(self, name, arguments, key, needle=None):
        result = self._tool(name, arguments, key)
        self.assertTrue(result["isError"], result)
        text = result["content"][0]["text"]
        if needle:
            self.assertIn(needle, text)
        return text

    def _key_for(self, user, name="key"):
        _, raw = ApiKeyService.create(user=user, name=name)
        return raw

    # -- lifecycle / plumbing ------------------------------------------------
    def test_requires_authentication(self):
        response = self.client.post(
            "/mcp",
            data=json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/list"}),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 401)

    def test_initialize_and_tools_list(self):
        init = self._call({"jsonrpc": "2.0", "id": 1, "method": "initialize"}, self.alice_key)
        self.assertEqual(init.json()["result"]["serverInfo"]["name"], "brainbox")

        listing = self._call({"jsonrpc": "2.0", "id": 2, "method": "tools/list"}, self.alice_key)
        names = {tool["name"] for tool in listing.json()["result"]["tools"]}
        self.assertIn("knowledge_search", names)
        self.assertIn("knowledge_get", names)
        self.assertIn("secret_use", names)
        self.assertIn("knowledge_transfer_ownership", names)
        self.assertIn("knowledge_take_over", names)
        self.assertIn("knowledge_my_workspace", names)

    def test_search_tool_is_permission_filtered(self):
        response = self._call(
            {
                "jsonrpc": "2.0",
                "id": 3,
                "method": "tools/call",
                "params": {"name": "knowledge_search", "arguments": {"query": "bicep"}},
            },
            self.bob_key,
        )
        text = response.json()["result"]["content"][0]["text"]
        self.assertEqual(json.loads(text)["count"], 0)

    def test_create_document_tool(self):
        response = self._call(
            {
                "jsonrpc": "2.0",
                "id": 4,
                "method": "tools/call",
                "params": {
                    "name": "knowledge_create_document",
                    "arguments": {
                        "workspace": str(self.workspace.pk),
                        "title": "New note",
                        "content": "# New note\n",
                        "path": "new.md",
                    },
                },
            },
            self.alice_key,
        )
        payload = json.loads(response.json()["result"]["content"][0]["text"])
        self.assertEqual(payload["title"], "New note")
        self.assertEqual(payload["tree_path"], "new.md")

    def test_create_document_by_node(self):
        project = ProjectService.create(
            workspace=self.workspace, name="Deploy", created_by=self.alice
        )
        response = self._call(
            {
                "jsonrpc": "2.0",
                "id": 41,
                "method": "tools/call",
                "params": {
                    "name": "knowledge_create_document",
                    "arguments": {
                        "workspace": str(self.workspace.pk),
                        "node": "Deploy/dotnet/x.md",
                        "title": "By node",
                        "content": "# By node\n",
                    },
                },
            },
            self.alice_key,
        )
        payload = json.loads(response.json()["result"]["content"][0]["text"])
        self.assertEqual(payload["tree_path"], "Deploy/dotnet/x.md")
        self.assertEqual(payload["path"], "dotnet/x.md")
        self.assertEqual(payload["project"], str(project.pk))

    def test_create_folder_and_move_by_node(self):
        project = ProjectService.create(
            workspace=self.workspace, name="Deploy", created_by=self.alice
        )
        arena = self._payload(
            "knowledge_create_folder",
            {"workspace": str(self.workspace.pk), "node": "Deploy/arena"},
            self.alice_key,
        )
        runbooks = self._payload(
            "knowledge_create_folder",
            {"workspace": str(self.workspace.pk), "node": "Deploy/runbooks"},
            self.alice_key,
        )
        self.assertEqual(runbooks["tree_path"], "Deploy/runbooks")
        self.assertEqual(arena["tree_path"], "Deploy/arena")

        document = DocumentService.create(
            workspace=self.workspace, project=project, title="Note",
            path="x.md", content="# X\n", created_by=self.alice,
        )
        moved = self._payload(
            "knowledge_move_document",
            {"document_id": str(document.pk), "node": "Deploy/arena"},
            self.alice_key,
        )
        self.assertEqual(moved["tree_path"], "Deploy/arena/x.md")

        nested = self._payload(
            "knowledge_move_folder",
            {"folder_id": runbooks["id"], "node": "Deploy/arena"},
            self.alice_key,
        )
        self.assertEqual(nested["tree_path"], "Deploy/arena/runbooks")

        # Omitting `node` moves the folder to its scope root.
        rooted = self._payload(
            "knowledge_move_folder", {"folder_id": runbooks["id"]}, self.alice_key
        )
        self.assertEqual(rooted["tree_path"], "Deploy/runbooks")

    def test_move_document_by_node_rejects_cross_project(self):
        project = ProjectService.create(
            workspace=self.workspace, name="Deploy", created_by=self.alice
        )
        ProjectService.create(
            workspace=self.workspace, name="Other", created_by=self.alice
        )
        document = DocumentService.create(
            workspace=self.workspace, project=project, title="Note",
            path="x.md", content="# X\n", created_by=self.alice,
        )
        self._payload(
            "knowledge_create_folder",
            {"workspace": str(self.workspace.pk), "node": "Other/sub"},
            self.alice_key,
        )
        self._assert_error(
            "knowledge_move_document",
            {"document_id": str(document.pk), "node": "Other/sub"},
            self.alice_key,
            needle="project",
        )

    def test_gateway_list_and_call(self):
        from apps.gateway.services import GatewayService

        target = GatewayService.create(
            name="GitHub",
            base_url="https://api.github.com",
            config={"auth": "none"},
            workspace=self.workspace,
            created_by=self.alice,
        )
        listing = self._payload("gateway_list", {}, self.alice_key)
        self.assertEqual([row["name"] for row in listing["targets"]], ["GitHub"])
        with patch(
            "apps.gateway.services.GatewayService._send",
            return_value=(200, {}, '{"ok": true}', False),
        ):
            result = self._payload(
                "gateway_call",
                {"target": str(target.pk), "method": "GET", "path": "user"},
                self.alice_key,
            )
        self.assertEqual(result["status"], 200)
        self.assertIn("ok", result["body"])

    def test_gateway_mcp_parses_an_sse_response(self):
        from apps.gateway.services import GatewayService

        target = GatewayService.create(
            name="RemoteMCP",
            base_url="https://mcp.example.com",
            kind="mcp",
            config={"auth": "none"},
            workspace=self.workspace,
            created_by=self.alice,
        )
        sse = (
            "event: message\n"
            'data: {"jsonrpc": "2.0", "id": 1, "result": {"tools": []}}\n\n'
        )
        with patch(
            "apps.gateway.services.GatewayService._send",
            return_value=(200, {"Content-Type": "text/event-stream"}, sse, False),
        ):
            result = self._payload(
                "gateway_mcp",
                {"target": str(target.pk), "list_tools": True},
                self.alice_key,
            )
        self.assertEqual(result["result"], {"tools": []})

    def test_gateway_target_lifecycle(self):
        created = self._payload(
            "knowledge_create_gateway_target",
            {
                "name": "GH",
                "base_url": "https://api.github.com",
                "workspace": str(self.workspace.pk),
                "config": {"auth": "none"},
            },
            self.alice_key,
        )
        self.assertEqual(created["name"], "GH")
        updated = self._payload(
            "knowledge_update_gateway_target",
            {"target": created["id"], "enabled": False},
            self.alice_key,
        )
        self.assertFalse(updated["enabled"])
        deleted = self._payload(
            "knowledge_delete_gateway_target", {"target": created["id"]}, self.alice_key
        )
        self.assertEqual(deleted["deleted"], "GH")

    def test_gateway_target_create_needs_write(self):
        self._assert_error(
            "knowledge_create_gateway_target",
            {
                "name": "X",
                "base_url": "https://api.github.com",
                "workspace": str(self.workspace.pk),
            },
            self.bob_key,
        )

    def test_resources_list_and_read(self):
        listing = self._call(
            {"jsonrpc": "2.0", "id": 50, "method": "resources/list", "params": {}},
            self.alice_key,
        ).json()["result"]
        uris = [row["uri"] for row in listing["resources"]]
        self.assertIn(f"brainbox://documents/{self.skill.pk}", uris)

        read = self._call(
            {
                "jsonrpc": "2.0",
                "id": 51,
                "method": "resources/read",
                "params": {"uri": f"brainbox://documents/{self.skill.pk}"},
            },
            self.alice_key,
        ).json()["result"]
        self.assertIn("Bicep skill", read["contents"][0]["text"])

    def test_resources_are_permission_filtered(self):
        listing = self._call(
            {"jsonrpc": "2.0", "id": 52, "method": "resources/list", "params": {}},
            self.bob_key,
        ).json()["result"]
        self.assertEqual(listing["resources"], [])

        read = self._call(
            {
                "jsonrpc": "2.0",
                "id": 53,
                "method": "resources/read",
                "params": {"uri": f"brainbox://documents/{self.skill.pk}"},
            },
            self.bob_key,
        ).json()
        # An unreadable uri and a missing one answer the same way.
        self.assertEqual(read["error"]["code"], -32602)

    def test_memory_view(self):
        from apps.memory.services import FactService

        document = DocumentService.create(
            workspace=self.workspace, title="Payroll", path="hr/payroll.md",
            content="---\nowner: Anna\n---\n# P\n", created_by=self.alice,
        )
        FactService.extract_document(document)
        view = self._payload("memory_view", {"subject": "anna"}, self.alice_key)
        self.assertEqual(view["subjects"][0]["subject"], "Anna")

    def test_memory_reflect(self):
        from unittest.mock import patch

        from apps.memory.services import FactService

        document = DocumentService.create(
            workspace=self.workspace, title="Payroll", path="hr/payroll.md",
            content="---\nowner: Anna\n---\n# P\n", created_by=self.alice,
        )
        FactService.extract_document(document)
        with patch("apps.knowledge.llm.get_llm_provider") as provider:
            provider.return_value.name = "openai"
            provider.return_value.generate.return_value = "Anna owns Payroll."
            result = self._payload("memory_reflect", {"subject": "Anna"}, self.alice_key)
        self.assertEqual(result["aggregate"], "Anna owns Payroll.")

    def test_webhook_and_saved_search_tools(self):
        from apps.gateway.services import GatewayService

        target = GatewayService.create(
            name="hook", base_url="https://hooks.example.com", created_by=self.alice
        )
        created = self._payload(
            "webhook_create",
            {
                "name": "CI",
                "target": str(target.pk),
                "events": ["document.status_changed"],
            },
            self.alice_key,
        )
        self.assertEqual(created["name"], "CI")
        listing = self._payload("webhook_list", {}, self.alice_key)
        self.assertEqual(len(listing["webhooks"]), 1)
        saved = self._payload(
            "saved_search_create", {"name": "w", "query": "deploy"}, self.alice_key
        )
        self.assertEqual(saved["query"], "deploy")

    def test_curator_scan_and_approve(self):
        DocumentService.create(
            workspace=self.workspace, title="A", path="a.md", content="# S\n",
            created_by=self.alice,
        )
        DocumentService.create(
            workspace=self.workspace, title="B", path="b.md", content="# S\n",
            created_by=self.alice,
        )
        scan = self._payload(
            "curator_scan", {"workspace": str(self.workspace.pk)}, self.alice_key
        )
        self.assertEqual(scan["created"], 1)
        listing = self._payload(
            "curator_list_proposals", {"workspace": str(self.workspace.pk)}, self.alice_key
        )
        self.assertEqual(listing["count"], 1)
        approved = self._payload(
            "curator_approve", {"proposal_id": listing["proposals"][0]["id"]}, self.alice_key
        )
        self.assertEqual(approved["status"], "applied")

    def test_document_comments(self):
        added = self._payload(
            "document_comment_add",
            {"document_id": str(self.skill.pk), "body": "nice"},
            self.alice_key,
        )
        self.assertEqual(added["body"], "nice")
        listing = self._payload(
            "document_comments", {"document_id": str(self.skill.pk)}, self.alice_key
        )
        self.assertEqual(len(listing["comments"]), 1)
        resolved = self._payload(
            "document_comment_resolve", {"comment_id": added["id"]}, self.alice_key
        )
        self.assertTrue(resolved["resolved"])

    def test_prompts_list_and_get(self):
        listed = self._call(
            {"jsonrpc": "2.0", "id": 54, "method": "prompts/list", "params": {}},
            self.alice_key,
        ).json()["result"]
        self.assertIn("deploy_runbook", [row["name"] for row in listed["prompts"]])

        got = self._call(
            {
                "jsonrpc": "2.0",
                "id": 55,
                "method": "prompts/get",
                "params": {"name": "deploy_runbook", "arguments": {"service": "azure"}},
            },
            self.alice_key,
        ).json()["result"]
        text = got["messages"][0]["content"]["text"]
        self.assertIn("azure", text)

    def test_unknown_tool_returns_error_content(self):
        response = self._call(
            {
                "jsonrpc": "2.0",
                "id": 5,
                "method": "tools/call",
                "params": {"name": "does_not_exist", "arguments": {}},
            },
            self.alice_key,
        )
        self.assertTrue(response.json()["result"]["isError"])

    # -- graph tools must not leak names -------------------------------------
    def _two_workspaces_with_a_link(self):
        """A link from a doc carol can read to a doc she cannot."""
        carol = User.objects.create_user("carol", "carol@example.com", "pw")
        vault = WorkspaceService.create(name="Payroll", created_by=self.alice)
        secret = DocumentService.create(
            workspace=vault,
            title="Salary table",
            path="hr/salaries.md",
            content="# Salary table\n",
            created_by=self.alice,
        )
        LinkService.create(
            source=self.skill.resource, target=secret.resource, created_by=self.alice
        )
        PermissionService.grant(
            self.workspace.resource,
            subject_type=SubjectType.USER,
            subject_id=carol.id,
            permission=Permission.READ,
            created_by=self.alice,
        )
        return carol, self._key_for(carol, "carol"), secret

    def test_follow_link_hides_the_name_of_an_inaccessible_neighbour(self):
        _, carol_key, secret = self._two_workspaces_with_a_link()

        result = self._tool(
            "knowledge_follow_link", {"resource_id": str(self.skill.resource_id)}, carol_key
        )
        payload = json.loads(result["content"][0]["text"])
        self.assertFalse(result.get("isError"), result)
        self.assertEqual(payload["count"], 0)
        self.assertEqual(payload["links"], [])
        # The whole point: not even the existence or the name of her neighbour.
        self.assertNotIn("Salary table", result["content"][0]["text"])
        self.assertNotIn(str(secret.resource_id), result["content"][0]["text"])

        # Opting in is the documented escape hatch, and it does leak the name.
        opted_in = self._payload(
            "knowledge_follow_link",
            {"resource_id": str(self.skill.resource_id), "include_inaccessible": True},
            carol_key,
        )
        self.assertEqual(opted_in["count"], 1)
        self.assertFalse(opted_in["links"][0]["accessible"])
        self.assertEqual(opted_in["links"][0]["name"], "Salary table")

        # Alice owns both sides, so she still sees the link.
        mine = self._payload(
            "knowledge_follow_link", {"resource_id": str(self.skill.resource_id)}, self.alice_key
        )
        self.assertEqual([row["name"] for row in mine["links"]], ["Salary table"])

    def test_get_related_hides_the_name_of_an_inaccessible_neighbour(self):
        _, carol_key, secret = self._two_workspaces_with_a_link()

        result = self._tool(
            "knowledge_get_related", {"resource_id": str(self.skill.resource_id)}, carol_key
        )
        payload = json.loads(result["content"][0]["text"])
        self.assertEqual(payload["count"], 0)
        self.assertNotIn(str(secret.resource_id), result["content"][0]["text"])

        opted_in = self._payload(
            "knowledge_get_related",
            {"resource_id": str(self.skill.resource_id), "include_inaccessible": True},
            carol_key,
        )
        self.assertEqual(opted_in["count"], 1)
        self.assertFalse(opted_in["related"][0]["accessible"])
        self.assertEqual(opted_in["related"][0]["name"], "Salary table")

    # -- gated admin tools ---------------------------------------------------
    def test_get_settings_is_staff_only(self):
        self._assert_error(
            "knowledge_get_settings", {}, self.bob_key, needle="Staff access required"
        )
        self.alice.is_staff = True
        self.alice.save(update_fields=["is_staff"])
        self.assertIn("settings", self._payload("knowledge_get_settings", {}, self.alice_key))

    def test_quality_metrics_is_superuser_only(self):
        # A staff-but-not-superuser caller is still refused: the REST
        # QualityView is IsAdminUser, and the two gates must not drift apart.
        self.alice.is_staff = True
        self.alice.save(update_fields=["is_staff"])
        self._assert_error("knowledge_quality_metrics", {}, self.alice_key)
        self._assert_error("knowledge_quality_metrics", {}, self.bob_key)

        self.alice.is_superuser = True
        self.alice.save(update_fields=["is_superuser"])
        self.assertIn("documents", self._payload("knowledge_quality_metrics", {}, self.alice_key))

    # -- ownership -----------------------------------------------------------
    def test_transfer_ownership_moves_the_owner_and_revokes_the_previous_admin(self):
        payload = self._payload(
            "knowledge_transfer_ownership",
            {"workspace": str(self.workspace.pk), "new_owner": "bob"},
            self.alice_key,
        )
        self.assertTrue(payload["transferred"])
        self.assertEqual(payload["owner"], str(self.bob.pk))
        self.assertEqual(payload["previous_owner"], str(self.alice.pk))

        self.workspace.refresh_from_db()
        self.assertEqual(self.workspace.owner, self.bob)
        resource = self.workspace.resource
        self.assertTrue(
            PermissionService.check(self.bob, resource, Permission.ADMIN)
        )
        self.assertFalse(
            PermissionService.check(self.alice, resource, Permission.ADMIN)
        )
        self.assertFalse(
            PermissionService.check(self.alice, resource, Permission.READ)
        )
        self.assertTrue(
            AuditEvent.objects.filter(
                action=AuditAction.CHANGE_PERMISSION,
                resource=resource,
                detail__type="ownership_transfer",
            ).exists()
        )

    def test_transfer_ownership_can_keep_a_weaker_grant(self):
        self._payload(
            "knowledge_transfer_ownership",
            {
                "workspace": self.workspace.slug,
                "new_owner": str(self.bob.pk),
                "keep_access": "write",
            },
            self.alice_key,
        )
        resource = self.workspace.resource
        self.workspace.refresh_from_db()
        self.assertEqual(self.workspace.owner, self.bob)
        self.assertTrue(PermissionService.check(self.alice, resource, Permission.WRITE))
        # "keep write" must be a real downgrade, not "keep admin".
        self.assertEqual(
            PermissionService.max_grantable(self.alice, resource), Permission.WRITE
        )

    def test_transfer_ownership_rejects_a_bogus_keep_access(self):
        self._assert_error(
            "knowledge_transfer_ownership",
            {"workspace": str(self.workspace.pk), "new_owner": "bob", "keep_access": "admin"},
            self.alice_key,
            needle="keep_access",
        )

    def test_transfer_ownership_is_denied_for_a_non_owner(self):
        self._assert_error(
            "knowledge_transfer_ownership",
            {"workspace": str(self.workspace.pk), "new_owner": "bob"},
            self.bob_key,
            needle="Permission denied",
        )
        self.workspace.refresh_from_db()
        self.assertEqual(self.workspace.owner, self.alice)

    def test_transfer_ownership_refuses_a_personal_workspace(self):
        personal = PersonalWorkspaceService.get_or_create(self.alice)
        self._assert_error(
            "knowledge_transfer_ownership",
            {"workspace": str(personal.pk), "new_owner": "bob"},
            self.alice_key,
            needle="Personal workspace",
        )
        personal.refresh_from_db()
        self.assertEqual(personal.owner, self.alice)

    def test_take_over_is_superuser_only_and_audited(self):
        vault = WorkspaceService.create(name="Vault", created_by=self.bob)
        self._assert_error(
            "knowledge_take_over",
            {"workspace": str(vault.pk)},
            self.alice_key,
            needle="Superuser access required",
        )

        root = User.objects.create_user(
            "root", "root@example.com", "pw", is_superuser=True, is_staff=True
        )
        payload = self._payload(
            "knowledge_take_over", {"workspace": str(vault.pk)}, self._key_for(root, "root")
        )
        self.assertTrue(payload["taken_over"])
        self.assertTrue(payload["audited"])
        self.assertEqual(payload["entry"]["permission"], Permission.ADMIN)
        self.assertTrue(PermissionService.check(root, vault.resource, Permission.ADMIN))
        # The owner is untouched: a takeover is a way in, not a way to lock out.
        vault.refresh_from_db()
        self.assertEqual(vault.owner, self.bob)
        self.assertTrue(AuditEvent.objects.filter(detail__type="takeover").exists())

    # -- Personal workspace --------------------------------------------------
    def test_personal_workspace_is_not_listable_by_anybody_else(self):
        personal = PersonalWorkspaceService.get_or_create(self.alice)
        alice_view = self._payload("knowledge_list_workspaces", {}, self.alice_key)
        mine = {row["id"]: row for row in alice_view["workspaces"]}
        self.assertIn(str(personal.pk), mine)
        self.assertTrue(mine[str(personal.pk)]["is_personal"])

        bob_view = self._payload("knowledge_list_workspaces", {}, self.bob_key)
        self.assertNotIn(str(personal.pk), {row["id"] for row in bob_view["workspaces"]})
        self.assertNotIn("Personal", json.dumps(bob_view))

        # And the read-only shortcut only ever resolves the caller's own.
        self.assertEqual(
            self._payload("knowledge_my_workspace", {}, self.alice_key)["workspace"]["id"],
            str(personal.pk),
        )
        self.assertIsNone(
            self._payload("knowledge_my_workspace", {}, self.bob_key)["workspace"]
        )

    # -- folder navigation ---------------------------------------------------
    def test_folder_listing_keeps_a_granted_deep_folder_navigable(self):
        carol = User.objects.create_user("carol", "carol@example.com", "pw")
        carol_key = self._key_for(carol, "carol")
        deep_note = DocumentService.create(
            workspace=self.workspace,
            title="Deep note",
            path="skills/azure/deep/note.md",
            content="# Deep note\n",
            created_by=self.alice,
        )
        DocumentService.create(
            workspace=self.workspace,
            title="Payroll note",
            path="hr/salaries.md",
            content="# Payroll note\n",
            created_by=self.alice,
        )
        deep = deep_note.resource.folder_resource()
        PermissionService.grant(
            deep,
            subject_type=SubjectType.USER,
            subject_id=carol.id,
            permission=Permission.READ,
            created_by=self.alice,
        )

        # Root: only the folder on the path down to the grant, not its siblings.
        root = self._payload(
            "knowledge_list_documents", {"workspace": str(self.workspace.pk)}, carol_key
        )
        self.assertEqual([row["path"] for row in root["folders"]], ["skills"])
        self.assertFalse(root["folders"][0]["readable"])
        # The grant inherits to the contents, so the deep note is readable; the
        # sibling folder's document is not, and never appears.
        self.assertEqual([doc["title"] for doc in root["documents"]], ["Deep note"])

        level = self._payload(
            "knowledge_list_documents",
            {"workspace": str(self.workspace.pk), "folder": "skills/azure"},
            carol_key,
        )
        # The path down to the grant plus the one readable child below it.
        self.assertEqual(
            [(row["path"], row["readable"]) for row in level["folders"]],
            [
                ("skills", False),
                ("skills/azure", False),
                ("skills/azure/deep", True),
            ],
        )

        listing = self._payload(
            "knowledge_list_documents",
            {"workspace": str(self.workspace.pk), "folder": "skills/azure/deep"},
            carol_key,
        )
        self.assertEqual([doc["title"] for doc in listing["documents"]], ["Deep note"])

        # Alice owns everything, so she still sees both top-level folders.
        alice_root = self._payload(
            "knowledge_list_documents", {"workspace": str(self.workspace.pk)}, self.alice_key
        )
        self.assertEqual(
            sorted(row["path"] for row in alice_root["folders"]), ["hr", "skills"]
        )

    def test_folder_listing_rejects_a_traversing_path(self):
        self._assert_error(
            "knowledge_list_documents",
            {"workspace": str(self.workspace.pk), "folder": "../../etc"},
            self.alice_key,
        )
