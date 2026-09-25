import json
import tempfile

from django.test import TestCase, override_settings

from apps.accounts.models import User
from apps.accounts.services import ApiKeyService
from apps.documents.models import DocumentStatus
from apps.documents.services import DocumentService
from apps.workspaces.services import WorkspaceService


@override_settings(KNOWLEDGE_DATA_ROOT=tempfile.mkdtemp())
class MCPTests(TestCase):
    def setUp(self):
        self.alice = User.objects.create_user("alice", "alice@example.com", "pw")
        self.bob = User.objects.create_user("bob", "bob@example.com", "pw")
        self.workspace = WorkspaceService.create(name="Company", created_by=self.alice)
        DocumentService.create(
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
