"""File content route + file browser page.

The content route is what ``apps.documents.embeds`` rewrites document image
references to, so these cover both halves: that it answers (with the right
headers, and a 404 for someone who may not read), and that it stays a 404 rather
than becoming a way to learn that a file exists.
"""

import tempfile

from django.test import TestCase, override_settings
from django.urls import reverse

from apps.accounts.models import User
from apps.audit.models import AuditAction, AuditEvent
from apps.documents.services import DocumentService
from apps.files.models import File
from apps.files.services import FileService
from apps.permissions.constants import Effect, Permission, SubjectType
from apps.permissions.services import PermissionService
from apps.workspaces.services import ProjectService, WorkspaceService

PNG = b"\x89PNG\r\n\x1a\n" + b"pixels"


@override_settings(KNOWLEDGE_DATA_ROOT=tempfile.mkdtemp())
class FileContentViewTests(TestCase):
    def setUp(self):
        self.alice = User.objects.create_user("alice", "alice@example.com", "pw")
        self.bob = User.objects.create_user("bob", "bob@example.com", "pw")
        self.workspace = WorkspaceService.create(name="Company", created_by=self.alice)
        self.project = ProjectService.create(
            workspace=self.workspace, name="Deploy", created_by=self.alice
        )
        self.stored = FileService.create(
            workspace=self.workspace,
            project=self.project,
            name="diagram.png",
            data=PNG,
            created_by=self.alice,
        )
        self.url = reverse("web:file_content", args=[self.stored.pk])

    def test_owner_gets_the_bytes_with_the_stored_mime_type(self):
        self.client.force_login(self.alice)
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(b"".join(response.streaming_content), PNG)
        self.assertEqual(response["Content-Type"], "image/png")
        self.assertEqual(response["Content-Length"], str(len(PNG)))
        self.assertEqual(response["X-Content-Type-Options"], "nosniff")
        self.assertIn("inline", response["Content-Disposition"])

    def test_read_is_written_to_the_audit_trail(self):
        self.client.force_login(self.alice)
        self.client.get(self.url)
        event = AuditEvent.objects.filter(action=AuditAction.READ).latest("timestamp")
        self.assertEqual(event.resource_id, self.stored.resource_id)
        self.assertEqual(event.detail["path"], "diagram.png")
        self.assertEqual(event.user_id, self.alice.id)

    def test_user_without_read_gets_404_not_403(self):
        self.client.force_login(self.bob)
        response = self.client.get(self.url)
        # 404, not 403: this URL is embedded in rendered documents, so the answer
        # must not distinguish "not yours" from "does not exist".
        self.assertEqual(response.status_code, 404)
        self.assertFalse(
            AuditEvent.objects.filter(action=AuditAction.READ).exists(),
            "a denied read must not be logged as a read",
        )

    def test_workspace_reader_with_a_deny_on_the_file_gets_404(self):
        """A DENY on the file beats the workspace grant it inherits."""
        PermissionService.grant(
            self.workspace.resource,
            subject_type=SubjectType.USER,
            subject_id=self.bob.id,
            permission=Permission.READ,
            created_by=self.alice,
        )
        PermissionService.grant(
            self.stored.resource,
            subject_type=SubjectType.USER,
            subject_id=self.bob.id,
            permission=Permission.READ,
            effect=Effect.DENY,
            created_by=self.alice,
        )
        self.client.force_login(self.bob)
        self.assertEqual(self.client.get(self.url).status_code, 404)

    def test_download_query_forces_attachment_with_quoted_name(self):
        self.client.force_login(self.alice)
        response = self.client.get(self.url + "?download=1")
        self.assertEqual(response.status_code, 200)
        self.assertIn('attachment; filename="diagram.png"', response["Content-Disposition"])

    def test_non_ascii_name_survives_the_disposition_header(self):
        stored = FileService.create(
            workspace=self.workspace,
            name="árvíztűrő.png",
            data=PNG,
            created_by=self.alice,
        )
        self.client.force_login(self.alice)
        response = self.client.get(reverse("web:file_content", args=[stored.pk]) + "?download=1")
        self.assertEqual(response.status_code, 200)
        self.assertIn("filename*=utf-8''", response["Content-Disposition"])

    def test_row_without_bytes_on_disk_is_404(self):
        FileService.storage_path(self.stored).unlink()
        self.client.force_login(self.alice)
        self.assertEqual(self.client.get(self.url).status_code, 404)

    def test_anonymous_is_redirected_to_login(self):
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 302)
        self.assertIn("/login/", response["Location"])

    def test_document_image_reference_points_at_the_content_route(self):
        document = DocumentService.create(
            workspace=self.workspace,
            project=self.project,
            title="Architektúra",
            path="docs/arch.md",
            content="![diagram](diagram.png)",
            created_by=self.alice,
        )
        self.client.force_login(self.alice)
        response = self.client.get(reverse("web:document_detail", args=[document.pk]))
        self.assertContains(response, f'src="{self.url}"')


@override_settings(KNOWLEDGE_DATA_ROOT=tempfile.mkdtemp())
class FileBrowserTests(TestCase):
    def setUp(self):
        self.alice = User.objects.create_user("alice", "alice@example.com", "pw")
        self.bob = User.objects.create_user("bob", "bob@example.com", "pw")
        self.workspace = WorkspaceService.create(name="Company", created_by=self.alice)
        self.project = ProjectService.create(
            workspace=self.workspace, name="Deploy", created_by=self.alice
        )
        self.open_file = FileService.create(
            workspace=self.workspace,
            project=self.project,
            name="diagram.png",
            data=PNG,
            created_by=self.alice,
        )
        # A file that is explicitly off limits, even to the workspace owner: a
        # DENY is the only thing that beats an inherited project grant, so it is
        # the honest way to build "you may see the folder, not this file".
        self.secret_file = FileService.create(
            workspace=self.workspace,
            project=self.project,
            name="salaries.png",
            data=PNG,
            created_by=self.alice,
        )
        # bob is a reader of one file, not of the project. A grant on a single
        # file inside is exactly the case `can_browse` exists to make reachable.
        PermissionService.grant(
            self.open_file.resource,
            subject_type=SubjectType.USER,
            subject_id=self.bob.id,
            permission=Permission.READ,
            created_by=self.alice,
        )
        # Granted before alice's own DENY below: a DENY read on a resource also
        # takes write on it away from *everyone*, including the owner, so nothing
        # further may be granted there afterwards (see PermissionService.grant).
        PermissionService.grant(
            self.secret_file.resource,
            subject_type=SubjectType.USER,
            subject_id=self.bob.id,
            permission=Permission.READ,
            effect=Effect.DENY,
            created_by=self.alice,
        )
        PermissionService.grant(
            self.secret_file.resource,
            subject_type=SubjectType.USER,
            subject_id=self.alice.id,
            permission=Permission.READ,
            effect=Effect.DENY,
            created_by=self.alice,
        )
        from apps.documents.models import DocumentFolder

        node = DocumentFolder.objects.get(resource_id=self.project.resource_id)
        self.project_url = reverse(
            "web:node_files", args=[self.workspace.slug, node.tree_path()]
        )

    def test_browser_lists_only_files_the_caller_may_read(self):
        self.client.force_login(self.alice)
        response = self.client.get(self.project_url)
        self.assertEqual(response.status_code, 200)
        names = [row["name"] for row in response.context["rows"] if row["type"] == "file"]
        self.assertEqual(names, ["diagram.png"])
        self.assertNotContains(response, "salaries.png")

    def test_image_row_gets_a_lazy_thumbnail_and_a_download_link(self):
        self.client.force_login(self.alice)
        response = self.client.get(self.project_url)
        content_url = reverse("web:file_content", args=[self.open_file.pk])
        self.assertContains(response, f'<img loading="lazy" src="{content_url}"')
        self.assertContains(response, f'href="{content_url}?download=1"')
        self.assertContains(response, "diagram.png")

    def test_workspace_reader_does_not_inherit_the_denied_file(self):
        self.client.force_login(self.bob)
        response = self.client.get(self.project_url)
        self.assertEqual(response.status_code, 200)
        names = [row["name"] for row in response.context["rows"] if row["type"] == "file"]
        self.assertEqual(names, ["diagram.png"])
        self.assertNotContains(response, "salaries.png")

    def test_groups_files_into_folders_from_the_path(self):
        nested = FileService.create(
            workspace=self.workspace,
            project=self.project,
            name="flow.svg",
            path="abra/flow.svg",
            data=b"<svg/>",
            created_by=self.alice,
        )
        self.client.force_login(self.alice)
        rows = self.client.get(self.project_url).context["rows"]
        self.assertEqual(
            [(row["type"], row["name"], row["depth"]) for row in rows],
            [
                ("dir", "abra", 0),
                ("file", "flow.svg", 1),
                ("file", "diagram.png", 0),
            ],
        )
        self.assertTrue(next(row for row in rows if row["name"] == "flow.svg")["is_image"])
        self.assertTrue(nested.pk)

    def test_size_is_rendered_human_readable(self):
        stored = FileService.create(
            workspace=self.workspace,
            project=self.project,
            name="big.bin",
            data=b"x" * (2 * 1024 * 1024 + 512),
            created_by=self.alice,
        )
        self.assertEqual(_human(stored.size), "2,0 MB")

    def test_stranger_cannot_open_the_page(self):
        stranger = User.objects.create_user("stranger", "stranger@example.com", "pw")
        self.client.force_login(stranger)
        self.assertEqual(self.client.get(self.project_url).status_code, 404)

    def test_upload_stores_the_file_and_nested_folders(self):
        from django.core.files.uploadedfile import SimpleUploadedFile

        self.client.force_login(self.alice)
        response = self.client.post(
            self.project_url,
            {
                "action": "upload",
                "folder": "abrak",
                "files": SimpleUploadedFile("logo.png", PNG, content_type="image/png"),
            },
        )
        self.assertRedirects(response, self.project_url)
        uploaded = File.objects.get(workspace=self.workspace, project=self.project, path="abrak/logo.png")
        self.assertEqual(uploaded.name, "logo.png")
        self.assertEqual(FileService.read_bytes(uploaded), PNG)

    def test_upload_needs_write_on_the_container(self):
        from django.core.files.uploadedfile import SimpleUploadedFile

        self.client.force_login(self.bob)
        self.client.post(
            self.project_url,
            {"action": "upload", "files": SimpleUploadedFile("x.png", PNG)},
        )
        self.assertFalse(
            File.objects.filter(workspace=self.workspace, name="x.png").exists()
        )

    def test_delete_requires_delete_on_the_file(self):
        self.client.force_login(self.bob)
        response = self.client.post(
            self.project_url, {"action": "delete", "file": str(self.open_file.pk)}
        )
        self.assertEqual(response.status_code, 302)
        self.assertTrue(File.objects.filter(pk=self.open_file.pk).exists())

    def test_delete_removes_the_row_and_the_bytes(self):
        self.client.force_login(self.alice)
        response = self.client.post(
            self.project_url, {"action": "delete", "file": str(self.open_file.pk)}
        )
        self.assertRedirects(response, self.project_url)
        self.assertFalse(File.objects.filter(pk=self.open_file.pk).exists())
        self.assertFalse(FileService.storage_path(self.open_file).exists())

    def test_delete_404s_for_an_unknown_file(self):
        self.client.force_login(self.alice)
        response = self.client.post(
            self.project_url,
            {"action": "delete", "file": "00000000-0000-0000-0000-000000000000"},
        )
        self.assertEqual(response.status_code, 404)


@override_settings(KNOWLEDGE_DATA_ROOT=tempfile.mkdtemp())
class WorkspaceFilesPageTests(TestCase):
    def test_workspace_scope_lists_its_own_files_only(self):
        alice = User.objects.create_user("alice", "alice@example.com", "pw")
        workspace = WorkspaceService.create(name="Company", created_by=alice)
        project = ProjectService.create(workspace=workspace, name="Deploy", created_by=alice)
        root_file = FileService.create(
            workspace=workspace, name="logo.png", data=PNG, created_by=alice
        )
        project_file = FileService.create(
            workspace=workspace, project=project, name="diagram.png", data=PNG, created_by=alice
        )
        self.client.force_login(alice)
        rows = self.client.get(
            reverse("web:workspace_files", args=[workspace.slug])
        ).context["rows"]
        self.assertEqual([row["file"].pk for row in rows if row["type"] == "file"], [root_file.pk])
        self.assertNotIn(project_file.pk, [row["file"].pk for row in rows if row["type"] == "file"])


def _human(size):
    from apps.web.views import _human_size

    return _human_size(size)


@override_settings(KNOWLEDGE_DATA_ROOT=tempfile.mkdtemp())
class AttachmentPlacementTests(TestCase):
    """An attachment belongs in the folder its path names, not on the root.

    It used to land on the project/workspace root whatever its path, so a folder
    page could not show it where it is - the file existed and its path said
    "abra/arch.png" while its container was the project.
    """

    def setUp(self):
        self.alice = User.objects.create_user("alice3", "a3@example.com", "pw")
        self.workspace = WorkspaceService.create(name="Attach", created_by=self.alice)
        self.project = ProjectService.create(
            workspace=self.workspace, name="Deploy", created_by=self.alice
        )

    def test_a_nested_attachment_lands_in_its_folder(self):
        from apps.documents.models import DocumentFolder

        stored = FileService.create(
            workspace=self.workspace,
            project=self.project,
            name="arch.png",
            path="abra/arch.png",
            data=PNG,
            created_by=self.alice,
        )
        folder = DocumentFolder.objects.get(workspace=self.workspace, path="abra")
        self.assertEqual(stored.folder_id, folder.pk)
        self.assertEqual(stored.resource.parent_id, folder.resource_id)

    def test_the_node_page_lists_the_attachment_with_the_documents(self):
        from apps.documents.models import DocumentFolder

        FileService.create(
            workspace=self.workspace,
            project=self.project,
            name="arch.png",
            path="abra/arch.png",
            data=PNG,
            created_by=self.alice,
        )
        DocumentService.create(
            workspace=self.workspace,
            project=self.project,
            title="Readme",
            path="abra/readme.md",
            content="# x",
            created_by=self.alice,
        )
        self.client.force_login(self.alice)
        node = DocumentFolder.objects.get(resource_id=self.project.resource_id)
        page = self.client.get(
            reverse("web:folder_detail", args=[self.workspace.slug, node.tree_path()])
        )
        rows = page.context["tree_rows"]
        self.assertIn("file", [row["type"] for row in rows])
        self.assertIn("arch.png", [row["name"] for row in rows])
        self.assertIn("Readme", [row["name"] for row in rows])