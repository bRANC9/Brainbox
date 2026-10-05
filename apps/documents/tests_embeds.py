import base64
import tempfile

from django.test import TestCase, override_settings

from apps.accounts.models import User
from apps.documents import embeds
from apps.documents.services import DocumentService
from apps.files.services import FileService
from apps.workspaces.services import ProjectService, WorkspaceService

PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAQAAAAECAYAAACp8Z5+AAAAFUlEQVR42mP8z8BQz0AEYBxV"
    "SF+FABJADveWkH6oAAAAAElFTkSuQmCC"
)


@override_settings(KNOWLEDGE_DATA_ROOT=tempfile.mkdtemp())
class RelativeReferenceScopeTests(TestCase):
    """A relative reference resolves inside the document's own workspace+project.

    ``path`` is scope-relative, so ``other.md`` and ``a.png`` exist in every
    project; the lookup used to ignore the scope and could rewrite a link to a
    same-named document or file somewhere else.
    """

    def setUp(self):
        self.user = User.objects.create_user("alice", "alice@example.com", "pw")
        self.workspace = WorkspaceService.create(name="Ecoform", created_by=self.user)
        self.p1 = ProjectService.create(
            workspace=self.workspace, name="P1", created_by=self.user
        )
        self.p2 = ProjectService.create(
            workspace=self.workspace, name="P2", created_by=self.user
        )
        self.source = DocumentService.create(
            workspace=self.workspace, project=self.p1, title="Src",
            path="src.md", content="# S\n", created_by=self.user,
        )
        # Same scope-relative names in two projects: the decoy in P2 is created
        # last, so an unscoped `.first()` would pick it.
        self.same = DocumentService.create(
            workspace=self.workspace, project=self.p1, title="Other",
            path="other.md", content="# A\n", created_by=self.user,
        )
        self.decoy = DocumentService.create(
            workspace=self.workspace, project=self.p2, title="Other",
            path="other.md", content="# B\n", created_by=self.user,
        )
        self.same_file = FileService.create(
            workspace=self.workspace, project=self.p1, name="a.png",
            path="a.png", data=PNG, created_by=self.user,
        )
        self.decoy_file = FileService.create(
            workspace=self.workspace, project=self.p2, name="a.png",
            path="a.png", data=PNG, created_by=self.user,
        )

    def test_relative_document_link_stays_in_scope(self):
        html = embeds.resolve(self.source, '<a href="other.md">x</a>')
        self.assertIn(f"/documents/{self.same.pk}/", html)
        self.assertNotIn(str(self.decoy.pk), html)

    def test_relative_image_stays_in_scope(self):
        html = embeds.resolve(self.source, '<img src="a.png">')
        self.assertIn(f"/files/{self.same_file.pk}/content/", html)
        self.assertNotIn(str(self.decoy_file.pk), html)
