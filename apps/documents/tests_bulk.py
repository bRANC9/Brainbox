import tempfile

from django.test import TestCase, override_settings

from apps.accounts.models import User
from apps.documents.models import Document
from apps.documents.services import DocumentService
from apps.workspaces.services import ProjectService, WorkspaceService


@override_settings(KNOWLEDGE_DATA_ROOT=tempfile.mkdtemp())
class BulkAndTemplateTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user("alice", "alice@example.com", "pw")
        self.workspace = WorkspaceService.create(name="Ecoform", created_by=self.user)
        self.project = ProjectService.create(
            workspace=self.workspace, name="Deploy", created_by=self.user
        )

    def test_bulk_create_multiple_files_into_folder(self):
        result = DocumentService.bulk_create_from_files(
            workspace=self.workspace,
            project=self.project,
            folder="deploy",
            uploads=[
                ("docker-compose deploy.md", b"# Docker\n\nUse az ecoform image-et.\n"),
                ("azure deploy.md", b"# Azure\n\nACA deploy.\n"),
                ("notes.txt", b"nota\n"),
            ],
            created_by=self.user,
        )
        self.assertEqual(len(result["created"]), 3)
        paths = {doc.path for doc in result["created"]}
        self.assertEqual(
            paths,
            {"deploy/docker-compose deploy.md", "deploy/azure deploy.md", "deploy/notes.txt"},
        )
        for doc in result["created"]:
            if doc.path.endswith(".md"):
                self.assertTrue(DocumentService.read_content(doc).startswith("# "))

    def test_bulk_skips_duplicates_instead_of_failing(self):
        uploads = [("a.md", b"# A\n")]
        DocumentService.bulk_create_from_files(
            workspace=self.workspace, project=self.project, folder="d", uploads=uploads,
            created_by=self.user,
        )
        second = DocumentService.bulk_create_from_files(
            workspace=self.workspace, project=self.project, folder="d", uploads=uploads,
            created_by=self.user,
        )
        self.assertEqual(len(second["created"]), 0)
        self.assertEqual(len(second["skipped"]), 1)

    def test_bulk_skips_non_utf8(self):
        result = DocumentService.bulk_create_from_files(
            workspace=self.workspace, project=self.project, folder="d",
            uploads=[("bad.md", b"\xff\xfe\x00binary")], created_by=self.user,
        )
        self.assertEqual(result["created"], [])
        self.assertEqual(result["skipped"][0]["reason"], "nem utf-8 szöveg")

    def test_bulk_preserves_folder_structure_from_directory_pick(self):
        result = DocumentService.bulk_create_from_files(
            workspace=self.workspace,
            project=self.project,
            folder="",
            uploads=[
                ("dotnet/Program.cs.md", b"# Program\n"),
                ("dotnet/legacy/old.md", b"# Old\n"),
                ("python/main.py.md", b"# Main\n"),
                ("django/settings.md", b"# Settings\n"),
            ],
            created_by=self.user,
        )
        self.assertEqual(len(result["created"]), 4)
        paths = {doc.path for doc in result["created"]}
        self.assertIn("dotnet/legacy/old.md", paths)
        self.assertIn("django/settings.md", paths)
        # intermediate folders registered
        from apps.documents.models import DocumentFolder

        self.assertTrue(DocumentFolder.objects.filter(path="dotnet/legacy").exists())

    def test_bulk_with_target_folder_prefixes_everything(self):
        result = DocumentService.bulk_create_from_files(
            workspace=self.workspace,
            project=self.project,
            folder="deploy",
            uploads=[("dotnet/x.md", b"# X\n"), ("python/y.md", b"# Y\n")],
            created_by=self.user,
        )
        self.assertEqual(
            {doc.path for doc in result["created"]},
            {"deploy/dotnet/x.md", "deploy/python/y.md"},
        )

    def test_bulk_strips_traversal_from_paths(self):
        result = DocumentService.bulk_create_from_files(
            workspace=self.workspace,
            project=self.project,
            folder="",
            uploads=[("../../etc/passwd.md", b"# evil\n")],
            created_by=self.user,
        )
        self.assertEqual({doc.path for doc in result["created"]}, {"etc/passwd.md"})

    def test_web_bulk_folder_pick_creates_stack_folders(self):
        from apps.documents.models import DocumentFolder

        self.client.force_login(self.user)
        url = f"/workspaces/{self.workspace.slug}/{self.project.slug}/folders/"
        response = self.client.post(url, {"paths": "dotnet\npython\ndjango\nlegacy/dotnet"})
        self.assertEqual(response.status_code, 302)
        created = set(DocumentFolder.objects.values_list("path", flat=True))
        self.assertIn("dotnet", created)
        self.assertIn("legacy/dotnet", created)

    def test_template_flag_and_instantiate(self):
        template = DocumentService.create(
            workspace=self.workspace,
            project=self.project,
            title="Deploy template",
            path="templates/deploy.md",
            content="---\ntype: pattern\n---\n# {{title}}\n\nDeploy: {{date}}\n",
            is_template=True,
            created_by=self.user,
        )
        self.assertTrue(template.is_template)

        instance = DocumentService.instantiate_template(
            template=template,
            workspace=self.workspace,
            project=self.project,
            title="Docker deploy",
            folder="deploy",
            created_by=self.user,
        )
        self.assertEqual(instance.path, "deploy/Docker deploy.md")
        content = DocumentService.read_content(instance)
        self.assertIn("# Docker deploy", content)
        self.assertIn("Deploy:", content)
        self.assertNotIn("{{title}}", content)
        self.assertEqual(instance.status, "draft")  # never approved straight from a template

    def test_web_bulk_upload_and_template_picker(self):
        from django.core.files.uploadedfile import SimpleUploadedFile

        self.client.force_login(self.user)
        url = f"/workspaces/{self.workspace.slug}/{self.project.slug}"
        response = self.client.post(
            url + "/bulk-upload/",
            {"folder": "deploy", "files": [SimpleUploadedFile("one.md", b"# One\n")]},
        )
        self.assertEqual(response.status_code, 302)
        self.assertTrue(Document.objects.filter(path="deploy/one.md").exists())

        DocumentService.create(
            workspace=self.workspace, project=self.project, title="Tpl",
            path="templates/t.md", content="# Tpl\n", is_template=True, created_by=self.user,
        )
        form = self.client.get(url + "/documents/new/")
        self.assertEqual(form.status_code, 200)
        self.assertIn(b"Vagy indulj egy sablonb", form.content)
