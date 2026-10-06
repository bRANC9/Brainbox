import tempfile
from datetime import date, timedelta

from django.test import TestCase, override_settings

from apps.accounts.models import User
from apps.documents.models import Document, DocumentStatus
from apps.documents.services import DocumentService
from apps.links.models import ResourceLink
from apps.workspaces.services import WorkspaceService

from .models import CuratorKind, CuratorProposal, CuratorStatus
from .services import CuratorService


@override_settings(KNOWLEDGE_DATA_ROOT=tempfile.mkdtemp())
class CuratorScanTests(TestCase):
    def setUp(self):
        self.alice = User.objects.create_user("alice", "alice@example.com", "pw")
        self.workspace = WorkspaceService.create(name="Company", created_by=self.alice)
        self.first = DocumentService.create(
            workspace=self.workspace, title="A", path="a.md",
            content="# Same\n", created_by=self.alice,
        )
        self.second = DocumentService.create(
            workspace=self.workspace, title="B", path="b.md",
            content="# Same\n", created_by=self.alice,
        )

    def test_scan_finds_exact_duplicates(self):
        result = CuratorService.scan_workspace(self.workspace)
        self.assertEqual(result["created"], 1)
        proposal = CuratorProposal.objects.get(kind=CuratorKind.DUPLICATE)
        self.assertEqual(proposal.payload["canonical_id"], str(self.first.pk))
        self.assertEqual(proposal.payload["duplicate_id"], str(self.second.pk))

    def test_rescan_is_idempotent(self):
        CuratorService.scan_workspace(self.workspace)
        CuratorService.scan_workspace(self.workspace)
        self.assertEqual(CuratorProposal.objects.count(), 1)

    def test_approve_archives_the_duplicate(self):
        CuratorService.scan_workspace(self.workspace)
        proposal = CuratorProposal.objects.get(kind=CuratorKind.DUPLICATE)
        CuratorService.decide(proposal, approve=True, user=self.alice)
        self.second.refresh_from_db()
        self.assertEqual(self.second.status, DocumentStatus.ARCHIVED)
        self.assertTrue(
            ResourceLink.objects.filter(
                source=self.second.resource, target=self.first.resource
            ).exists()
        )
        proposal.refresh_from_db()
        self.assertEqual(proposal.status, CuratorStatus.APPLIED)

    def test_reject_leaves_the_document_alone(self):
        CuratorService.scan_workspace(self.workspace)
        proposal = CuratorProposal.objects.get(kind=CuratorKind.DUPLICATE)
        CuratorService.decide(proposal, approve=False, user=self.alice)
        self.second.refresh_from_db()
        self.assertNotEqual(self.second.status, DocumentStatus.ARCHIVED)

    def test_a_decided_proposal_is_not_reopened(self):
        CuratorService.scan_workspace(self.workspace)
        proposal = CuratorProposal.objects.get(kind=CuratorKind.DUPLICATE)
        CuratorService.decide(proposal, approve=False, user=self.alice)
        CuratorService.scan_workspace(self.workspace)
        proposal.refresh_from_db()
        self.assertEqual(proposal.status, CuratorStatus.REJECTED)

    def test_stale_review_goes_back_to_draft(self):
        past = (date.today() - timedelta(days=30)).isoformat()
        DocumentService.create(
            workspace=self.workspace, title="Old", path="old.md",
            content=f"---\nreview_by: {past}\n---\n# Old\n",
            status="approved", created_by=self.alice,
        )
        CuratorService.scan_workspace(self.workspace)
        proposal = CuratorProposal.objects.get(kind=CuratorKind.STALE)
        CuratorService.decide(proposal, approve=True, user=self.alice)
        document = Document.objects.get(title="Old")
        document.refresh_from_db()
        self.assertEqual(document.status, DocumentStatus.DRAFT)
