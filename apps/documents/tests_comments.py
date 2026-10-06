import tempfile

from django.core.exceptions import PermissionDenied, ValidationError
from django.test import TestCase, override_settings

from apps.accounts.models import User
from apps.documents.comments import CommentService
from apps.documents.services import DocumentService
from apps.workspaces.services import WorkspaceService


@override_settings(KNOWLEDGE_DATA_ROOT=tempfile.mkdtemp())
class CommentServiceTests(TestCase):
    def setUp(self):
        self.alice = User.objects.create_user("alice", "alice@example.com", "pw")
        self.bob = User.objects.create_user("bob", "bob@example.com", "pw")
        self.workspace = WorkspaceService.create(name="Company", created_by=self.alice)
        self.document = DocumentService.create(
            workspace=self.workspace, title="D", path="d.md", content="# D\n",
            created_by=self.alice,
        )

    def test_only_readers_may_comment(self):
        with self.assertRaises(PermissionDenied):
            CommentService.add(document=self.document, author=self.bob, body="hi")

    def test_the_author_may_resolve_and_a_stranger_may_not(self):
        comment = CommentService.add(document=self.document, author=self.alice, body="hi")
        with self.assertRaises(PermissionDenied):
            CommentService.set_resolved(comment=comment, user=self.bob, resolved=False)
        CommentService.set_resolved(comment=comment, user=self.alice, resolved=True)
        comment.refresh_from_db()
        self.assertTrue(comment.resolved)

    def test_an_empty_body_is_rejected(self):
        with self.assertRaises(ValidationError):
            CommentService.add(document=self.document, author=self.alice, body="   ")
