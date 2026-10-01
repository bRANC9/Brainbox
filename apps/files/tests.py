import tempfile

from django.test import TestCase, override_settings

from apps.accounts.models import User
from apps.files.models import File
from apps.files.services import FileService
from apps.workspaces.services import ProjectService, WorkspaceService


@override_settings(KNOWLEDGE_DATA_ROOT=tempfile.mkdtemp())
class FileServiceTests(TestCase):
    def setUp(self):
        self.alice = User.objects.create_user("alice", "alice@example.com", "pw")
        self.workspace = WorkspaceService.create(name="Company", created_by=self.alice)
        self.project = ProjectService.create(
            workspace=self.workspace, name="Deploy", created_by=self.alice
        )

    def test_create_stores_bytes_and_metadata(self):
        stored = FileService.create(
            workspace=self.workspace,
            project=self.project,
            name="diagram.png",
            data=b"\x89PNG\r\n",
            created_by=self.alice,
        )
        self.assertEqual(stored.mime_type, "image/png")
        self.assertEqual(stored.size, 6)
        self.assertEqual(len(stored.checksum), 64)
        self.assertEqual(FileService.read_bytes(stored), b"\x89PNG\r\n")
        self.assertEqual(stored.path, "diagram.png")  # path is relative to the files/ root

    def test_duplicate_path_is_rejected(self):
        from django.core.exceptions import ValidationError

        FileService.create(
            workspace=self.workspace, name="a.txt", data=b"x", created_by=self.alice
        )
        with self.assertRaises(ValidationError):
            FileService.create(
                workspace=self.workspace, name="a.txt", data=b"y", created_by=self.alice
            )

    def test_update_creates_new_version(self):
        stored = FileService.create(
            workspace=self.workspace, name="a.txt", data=b"one", created_by=self.alice
        )
        updated = FileService.update_content(
            stored_file=stored, data=b"two", user=self.alice
        )
        self.assertEqual(updated.current_version, 2)
        self.assertEqual(FileService.read_bytes(updated), b"two")
        self.assertEqual(updated.versions.count(), 2)

    def test_changelog_checksum_changes(self):
        stored = FileService.create(
            workspace=self.workspace, name="a.txt", data=b"one", created_by=self.alice
        )
        before = stored.checksum
        FileService.update_content(stored_file=stored, data=b"different", user=self.alice)
        stored.refresh_from_db()
        self.assertNotEqual(stored.checksum, before)

    def test_delete_removes_file_from_disk(self):
        stored = FileService.create(
            workspace=self.workspace, name="gone.txt", data=b"x", created_by=self.alice
        )
        path = FileService.storage_path(stored)
        self.assertTrue(path.exists())
        FileService.delete(stored_file=stored, user=self.alice)
        self.assertFalse(path.exists())
        self.assertFalse(File.objects.filter(pk=stored.pk).exists())

    def test_unknown_mime_defaults_to_octet_stream(self):
        stored = FileService.create(
            workspace=self.workspace, name="weird.zzz", data=b"x", created_by=self.alice
        )
        self.assertEqual(stored.mime_type, "application/octet-stream")