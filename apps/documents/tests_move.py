import json
import tempfile
from pathlib import Path

from django.test import TestCase, override_settings

from apps.accounts.models import User
from apps.documents.folders import create_folder, move_folder
from apps.documents.models import Document, DocumentFolder
from apps.documents.services import DocumentService
from apps.workspaces.services import ProjectService, WorkspaceService


@override_settings(KNOWLEDGE_DATA_ROOT=tempfile.mkdtemp())
class MoveTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user("alice", "alice@example.com", "pw")
        self.workspace = WorkspaceService.create(name="Ecoform", created_by=self.user)
        self.project = ProjectService.create(
            workspace=self.workspace, name="Deploy", created_by=self.user
        )
        self.doc = DocumentService.create(
            workspace=self.workspace,
            project=self.project,
            title="Docker deploy",
            content="# Docker deploy\n",
            path="x.md",
            created_by=self.user,
        )

    def test_move_document_relocates_file_and_path(self):
        old_file = DocumentService.storage_path(self.doc)
        DocumentService.move(self.doc, "dotnet/docker.md", user=self.user)
        self.doc.refresh_from_db()
        new_file = DocumentService.storage_path(self.doc)
        self.assertEqual(self.doc.path, "dotnet/docker.md")
        self.assertTrue(new_file.exists())
        self.assertFalse(old_file.exists())
        # parent folder auto-registered
        self.assertTrue(DocumentFolder.objects.filter(path="dotnet").exists())

    def test_move_folder_relocates_children_on_disk_and_db(self):
        create_folder(workspace=self.workspace, project=self.project, path="dotnet", created_by=self.user)
        DocumentService.move(self.doc, "dotnet/legacy/deep.md", user=self.user)
        create_folder(workspace=self.workspace, project=self.project, path="python", created_by=self.user)

        dotnet = DocumentFolder.objects.get(path="dotnet")
        move_folder(folder=dotnet, new_parent="python", user=self.user)

        self.doc.refresh_from_db()
        self.assertEqual(self.doc.path, "python/dotnet/legacy/deep.md")
        moved_file = DocumentService.storage_path(self.doc)
        self.assertTrue(moved_file.exists())
        self.assertTrue(DocumentFolder.objects.filter(path="python/dotnet").exists())
        self.assertTrue(DocumentFolder.objects.filter(path="python/dotnet/legacy").exists())
        self.assertFalse(DocumentFolder.objects.filter(path="dotnet").exists())

    def test_move_folder_into_itself_rejected(self):
        create_folder(workspace=self.workspace, project=self.project, path="a", created_by=self.user)
        create_folder(workspace=self.workspace, project=self.project, path="a/b", created_by=self.user)
        child = DocumentFolder.objects.get(path="a/b")
        from django.core.exceptions import ValidationError

        with self.assertRaises(ValidationError):
            move_folder(folder=child, new_parent="a/b")

    def test_web_tree_move_endpoint(self):
        create_folder(workspace=self.workspace, project=self.project, path="dotnet", created_by=self.user)
        self.client.force_login(self.user)
        response = self.client.post(
            "/tree/move/",
            data=json.dumps(
                {
                    "type": "document",
                    "id": str(self.doc.pk),
                    # A tree path, not a scope-relative one: the endpoint derives
                    # the workspace from the moved document.
                    "target": f"{self.project.name}/dotnet",
                }
            ),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 200)
        self.doc.refresh_from_db()
        self.assertEqual(self.doc.path, "dotnet/x.md")

    def test_web_tree_move_folder_by_tree_path(self):
        create_folder(workspace=self.workspace, project=self.project, path="dotnet", created_by=self.user)
        create_folder(workspace=self.workspace, project=self.project, path="python", created_by=self.user)
        folder = DocumentFolder.objects.get(name="dotnet")
        self.client.force_login(self.user)
        response = self.client.post(
            "/tree/move/",
            data=json.dumps(
                {
                    "type": "folder",
                    "id": str(folder.pk),
                    "target": f"{self.project.name}/python",
                }
            ),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 200)
        folder.refresh_from_db()
        self.assertEqual(folder.path, "python/dotnet")

    def test_web_tree_folder_op_renames_by_tree_path(self):
        create_folder(workspace=self.workspace, project=self.project, path="dotnet", created_by=self.user)
        self.client.force_login(self.user)
        response = self.client.post(
            "/tree/folder-op/",
            data=json.dumps(
                {
                    "op": "rename",
                    "workspace": self.workspace.slug,
                    "node": f"{self.project.name}/dotnet",
                    "name": "dotnetcore",
                }
            ),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 200)
        folder = DocumentFolder.objects.get(name="dotnetcore")
        self.assertEqual(folder.path, "dotnetcore")

    def test_web_tree_folder_op_deletes_by_tree_path(self):
        create_folder(workspace=self.workspace, project=self.project, path="dotnet", created_by=self.user)
        self.client.force_login(self.user)
        response = self.client.post(
            "/tree/folder-op/",
            data=json.dumps(
                {
                    "op": "delete",
                    "workspace": self.workspace.slug,
                    "node": f"{self.project.name}/dotnet",
                }
            ),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 200)
        self.assertFalse(DocumentFolder.objects.filter(name="dotnet").exists())

    def test_api_move_action(self):
        from rest_framework.test import APIClient

        client = APIClient()
        client.force_authenticate(self.user)
        response = client.post(
            f"/api/v1/documents/{self.doc.pk}/move/", {"folder": "django"}, format="json"
        )
        self.assertEqual(response.status_code, 200)
        self.doc.refresh_from_db()
        self.assertEqual(self.doc.path, "django/x.md")
        self.assertTrue(Document.objects.filter(path="django/x.md").exists())

    def test_edit_form_path_change_moves_document(self):
        self.client.force_login(self.user)
        self.client.post(
            f"/documents/{self.doc.pk}/edit/",
            {"title": "Docker deploy", "path": "dotnet/edited.md", "content": "# x\n"},
        )
        self.doc.refresh_from_db()
        self.assertEqual(self.doc.path, "dotnet/edited.md")
        self.assertTrue(Path(DocumentService.storage_path(self.doc)).exists())
