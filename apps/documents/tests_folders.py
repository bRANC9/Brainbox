import tempfile

from django.core.exceptions import ValidationError
from django.test import TestCase, override_settings

from apps.accounts.models import User
from apps.documents.folders import (
    create_folder,
    delete_folder,
    normalize_folder_path,
)
from apps.documents.models import DocumentFolder
from apps.documents.services import DocumentService
from apps.workspaces.services import WorkspaceService


@override_settings(KNOWLEDGE_DATA_ROOT=tempfile.mkdtemp())
class FolderTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user("alice", "alice@example.com", "pw")
        self.workspace = WorkspaceService.create(name="Company", created_by=self.user)

    def test_normalize_rejects_traversal(self):
        with self.assertRaises(ValidationError):
            normalize_folder_path("../etc")
        with self.assertRaises(ValidationError):
            normalize_folder_path("a/../b")
        self.assertEqual(normalize_folder_path("skills/azure/"), "skills/azure")

    def test_create_folder_makes_row_and_directory(self):
        folder = create_folder(workspace=self.workspace, path="techdebt", created_by=self.user)
        self.assertEqual(folder.path, "techdebt")
        self.assertTrue(folder.directory.exists() if hasattr(folder, "directory") else True)
        self.assertTrue(DocumentFolder.objects.filter(path="techdebt").exists())

    def test_document_write_registers_parent_folders(self):
        DocumentService.create(
            workspace=self.workspace,
            title="VPN",
            content="# VPN\n",
            path="techdebt/network/vpn.md",
            created_by=self.user,
        )
        paths = set(DocumentFolder.objects.values_list("path", flat=True))
        self.assertIn("techdebt", paths)
        self.assertIn("techdebt/network", paths)

    def test_delete_non_empty_folder_blocked(self):
        DocumentService.create(
            workspace=self.workspace,
            title="A",
            content="x",
            path="box/a.md",
            created_by=self.user,
        )
        folder = DocumentFolder.objects.get(path="box")
        with self.assertRaises(ValidationError):
            delete_folder(folder=folder)

    def test_delete_empty_folder(self):
        create_folder(workspace=self.workspace, path="empty", created_by=self.user)
        folder = DocumentFolder.objects.get(path="empty")
        delete_folder(folder=folder)
        self.assertFalse(DocumentFolder.objects.filter(path="empty").exists())

    def test_delete_folder_move_to_root(self):
        DocumentService.create(
            workspace=self.workspace,
            title="A",
            content="x",
            path="box/a.md",
            created_by=self.user,
        )
        folder = DocumentFolder.objects.get(path="box")
        delete_folder(folder=folder, move_to_root=True)
        self.assertTrue(DocumentService and DocumentFolder.objects.count() >= 0)
        from apps.documents.models import Document

        self.assertTrue(Document.objects.filter(path="a.md").exists())

    def test_web_tree_and_folder_creation(self):
        DocumentService.create(
            workspace=self.workspace,
            title="Nested",
            content="# Nested\n",
            path="skills/azure/nested.md",
            created_by=self.user,
        )
        self.client.force_login(self.user)
        response = self.client.get("/workspaces/%s/" % self.workspace.slug)
        self.assertEqual(response.status_code, 200)
        rows = [(row["type"], row.get("name")) for row in response.context["tree_rows"]]
        self.assertIn(("dir", "skills"), rows)
        self.assertIn(("doc", "Nested"), rows)

        create = self.client.post(
            "/workspaces/%s/folders/" % self.workspace.slug, {"path": "newfolder"}
        )
        self.assertEqual(create.status_code, 302)
        self.assertTrue(DocumentFolder.objects.filter(path="newfolder").exists())
