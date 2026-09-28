import tempfile
from datetime import date

from django.test import TestCase, override_settings

from apps.accounts.models import User
from apps.deadlines.models import DeadlineStatus, KnowledgeDeadline
from apps.deadlines.services import extract_from_content
from apps.documents.services import DocumentService
from apps.workspaces.services import WorkspaceService


class ExtractionTests(TestCase):
    def test_frontmatter_deadline(self):
        items = extract_from_content(
            "---\ntitle: Tech debt\ndeadline: 2026-10-15\n---\n# Tech debt\nBody\n"
        )
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["due_date"], date(2026, 10, 15))
        self.assertEqual(items[0]["source"], "frontmatter")
        self.assertEqual(items[0]["confidence"], 100)

    def test_frontmatter_due_quoted_hungarian(self):
        items = extract_from_content('---\ndue: "2026.11.02."\n---\nBody\n')
        self.assertEqual(items[0]["due_date"], date(2026, 11, 2))

    def test_inline_keyword_date(self):
        content = "# Review notes\n\n- 2026-12-01 határidő: kódellenőrzés\n"
        items = extract_from_content(content)
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["due_date"], date(2026, 12, 1))
        self.assertEqual(items[0]["title"], "Review notes")

    def test_no_deadline_without_keyword(self):
        # A bare date must not create a deadline (no false positives).
        items = extract_from_content("# Changelog\n\nReleased 2026-01-05.\nUpdated 2026-03-09.\n")
        self.assertEqual(items, [])

    def test_code_block_is_skipped(self):
        content = "# Notes\n\n```python\n# deadline: 2026-05-05\n```\n"
        self.assertEqual(extract_from_content(content), [])

    def test_keyword_without_date_creates_nothing(self):
        self.assertEqual(extract_from_content("# Notes\n\nWe must set a deadline soon.\n"), [])


@override_settings(KNOWLEDGE_DATA_ROOT=tempfile.mkdtemp())
class DeadlineLifecycleTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user("alice", "alice@example.com", "pw")
        self.workspace = WorkspaceService.create(name="Company", created_by=self.user)

    def test_document_write_extracts_deadlines(self):
        doc = DocumentService.create(
            workspace=self.workspace,
            title="Tech debt",
            content="# Tech debt\n\n- deadline: 2026-10-15\n",
            path="tech-debt.md",
            created_by=self.user,
        )
        self.assertTrue(KnowledgeDeadline.objects.filter(document=doc).exists())
        self.assertEqual(KnowledgeDeadline.objects.filter(document=doc).count(), 1)

    def test_update_removes_stale_deadline(self):
        doc = DocumentService.create(
            workspace=self.workspace,
            title="TD",
            content="# TD\n\ndeadline: 2026-10-15\n",
            path="td.md",
            created_by=self.user,
        )
        self.assertEqual(KnowledgeDeadline.objects.filter(document=doc).count(), 1)
        DocumentService.update_content(document=doc, content="# TD\n\nno dates here\n", user=self.user)
        self.assertEqual(KnowledgeDeadline.objects.filter(document=doc).count(), 0)

    def test_manual_deadline_survives_rebuild(self):
        doc = DocumentService.create(
            workspace=self.workspace, title="M", content="# M\n", path="m.md", created_by=self.user
        )
        KnowledgeDeadline.objects.create(
            document=doc,
            resource=doc.resource,
            workspace=self.workspace,
            project=None,
            title="manual",
            due_date=date(2030, 1, 1),
            status=DeadlineStatus.OPEN,
            source="manual",
        )
        DocumentService.update_content(document=doc, content="# M\n\nupdated\n", user=self.user)
        self.assertTrue(KnowledgeDeadline.objects.filter(document=doc, source="manual").exists())

    def test_overdue_flag(self):
        doc = DocumentService.create(
            workspace=self.workspace,
            title="Past",
            content="# Past\n\ndeadline: 2020-01-01\n",
            path="past.md",
            created_by=self.user,
        )
        deadline = KnowledgeDeadline.objects.get(document=doc)
        self.assertTrue(deadline.is_overdue)
