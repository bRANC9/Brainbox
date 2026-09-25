import tempfile

from django.test import override_settings
from rest_framework import status
from rest_framework.test import APITestCase

from apps.accounts.models import User
from apps.accounts.services import ApiKeyService
from apps.documents.services import DocumentService
from apps.permissions.constants import Effect, Permission
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
