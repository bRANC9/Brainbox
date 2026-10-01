import tempfile
from pathlib import Path

from django.test import TestCase, override_settings

from apps.resources.models import ResourceType
from apps.resources.services import ResourceService
from apps.resources.storage import LocalStorage, StorageError, get_storage, resolve_path


class StoragePathValidationTests(TestCase):
    """The storage adapter must never let a path escape its roots."""

    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.storage = LocalStorage(self.root)

    def test_rejects_traversal(self):
        # LocalStorage validates against its own root: a path that escapes that
        # root is refused. (Escaping the knowledge root is additionally checked
        # in ResolvePathTests, which is where API/form input arrives.)
        for evil in ("../../etc/passwd", "..\\..\\windows\\system32", "/etc/passwd"):
            with self.assertRaises(StorageError):
                self.storage.validate(self.root / evil)

    def test_accepts_paths_inside_root(self):
        inside = self.root / "workspaces" / "abc" / "doc.md"
        self.assertEqual(self.storage.validate(inside), inside.resolve())

    def test_write_text_stays_inside(self):
        target = self.root / "workspaces" / "w" / "documents" / "a.md"
        self.storage.write_text(target, "hello")
        self.assertEqual(target.read_text(encoding="utf-8"), "hello")

    def test_write_text_refuses_escape(self):
        with self.assertRaises(StorageError):
            self.storage.write_text(self.root / ".." / "escaped.md", "x")


@override_settings(KNOWLEDGE_DATA_ROOT=tempfile.mkdtemp())
class ResolvePathTests(TestCase):
    def setUp(self):
        from apps.accounts.models import User
        from apps.workspaces.services import WorkspaceService

        self.user = User.objects.create_user("alice", "alice@example.com", "pw")
        self.workspace = WorkspaceService.create(name="Company", created_by=self.user)

    def test_local_layout(self):
        path = resolve_path(
            workspace=self.workspace, project=None, kind="documents", rel_path="a/b.md"
        )
        self.assertIn("workspaces", path.parts)
        self.assertIn(str(self.workspace.pk), path.parts)
        self.assertEqual(path.parts[-3:], ("documents", "a", "b.md"))

    def test_traversal_is_blocked(self):
        for evil in ("../../../etc/passwd", "..\\..\\windows\\system32", "/etc/passwd"):
            with self.assertRaises(StorageError):
                resolve_path(
                    workspace=self.workspace, project=None, kind="documents", rel_path=evil
                )

    def test_get_storage_is_cached_per_root(self):
        self.assertIs(get_storage(), get_storage())


class ResourceServiceTests(TestCase):
    def test_create_sets_type_and_parent(self):
        parent = ResourceService.create(resource_type=ResourceType.WORKSPACE, name="Company")
        child = ResourceService.create(
            resource_type=ResourceType.PROJECT, name="Azure", parent=parent
        )
        self.assertEqual(child.resource_type, ResourceType.PROJECT)
        self.assertEqual(child.parent_id, parent.pk)

    def test_ancestors_walks_up(self):
        a = ResourceService.create(resource_type=ResourceType.WORKSPACE, name="a")
        b = ResourceService.create(resource_type=ResourceType.PROJECT, name="b", parent=a)
        c = ResourceService.create(resource_type=ResourceType.DOCUMENT, name="c", parent=b)
        chain = list(c.ancestors())
        self.assertEqual([r.pk for r in chain], [c.pk, b.pk, a.pk])
        self.assertEqual(c.ancestor_of_type(ResourceType.WORKSPACE).pk, a.pk)
        # a resource is its own ancestor; a child has no ancestor of its own kind
        self.assertEqual(a.ancestor_of_type(ResourceType.WORKSPACE).pk, a.pk)
        self.assertIsNone(b.ancestor_of_type(ResourceType.DOCUMENT))