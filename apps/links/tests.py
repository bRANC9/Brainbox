import tempfile

from django.test import TestCase, override_settings

from apps.accounts.models import User
from apps.documents.services import DocumentService
from apps.links.services import LinkService
from apps.workspaces.services import WorkspaceService


@override_settings(KNOWLEDGE_DATA_ROOT=tempfile.mkdtemp())
class LinkResolutionTests(TestCase):
    def setUp(self):
        self.alice = User.objects.create_user("alice", "alice@example.com", "pw")
        self.workspace = WorkspaceService.create(name="Company", created_by=self.alice)

    def _doc(self, title, path, content=""):
        return DocumentService.create(
            workspace=self.workspace,
            title=title,
            path=path,
            content=content or f"# {title}\n",
            created_by=self.alice,
        )

    def test_wikilink_by_basename(self):
        target = self._doc("Bicep", "skills/bicep.md")
        source = self._doc("Guide", "guide.md", "See [[bicep]] and [[Bicep|the guide]].")
        # Both references resolve to the same target and are stored once.
        links = list(source.resource.outgoing_links.all())
        self.assertEqual(len(links), 1)
        self.assertEqual(links[0].target_id, target.resource_id)

    def test_wikilink_by_path(self):
        self._doc("Networking", "skills/azure/networking.md")
        source = self._doc("Ref", "ref.md", "See [[skills/azure/networking]].")
        self.assertEqual(source.resource.outgoing_links.count(), 1)

    def test_relative_markdown_link(self):
        target = self._doc("Other", "skills/other.md")
        source = self._doc("Src", "skills/src.md", "See [other](other.md).")
        self.assertEqual(source.resource.outgoing_links.count(), 1)
        self.assertEqual(source.resource.outgoing_links.first().target_id, target.resource_id)

    def test_heading_and_alias_are_stripped(self):
        self._doc("Target", "skills/target.md")
        source = self._doc("S", "s.md", "[[target#Section|Alias]]")
        self.assertEqual(source.resource.outgoing_links.count(), 1)

    def test_unresolved_link_is_skipped(self):
        source = self._doc("S", "s.md", "[[nowhere]]")
        self.assertEqual(source.resource.outgoing_links.count(), 0)

    def test_external_url_is_not_a_link(self):
        source = self._doc("S", "s.md", "[x](https://example.com/a.md)")
        self.assertEqual(source.resource.outgoing_links.count(), 0)

    def test_self_link_is_ignored(self):
        doc = self._doc("Self", "self.md", "[[self]]")
        self.assertEqual(doc.resource.outgoing_links.count(), 0)

    def test_edit_removes_stale_links(self):
        self._doc("Target", "skills/target.md")
        source = self._doc("S", "s.md", "[[target]]")
        self.assertEqual(source.resource.outgoing_links.count(), 1)
        DocumentService.update_content(document=source, content="no links now", user=self.alice)
        self.assertEqual(source.resource.outgoing_links.count(), 0)

    def test_links_are_deduped(self):
        target = self._doc("T", "t.md")
        source = self._doc("S", "s.md", "[[t]] [[t]] [[t]]")
        self.assertEqual(source.resource.outgoing_links.count(), 1)
        self.assertEqual(source.resource.outgoing_links.first().target_id, target.resource_id)

    def test_create_and_delete(self):
        a, b = self._doc("A", "a.md"), self._doc("B", "b.md")
        LinkService.create(source=a.resource, target=b.resource, created_by=self.alice)
        self.assertEqual(a.resource.outgoing_links.count(), 1)
        deleted = LinkService.delete(source=a.resource)
        self.assertEqual(deleted, 1)
        self.assertEqual(a.resource.outgoing_links.count(), 0)