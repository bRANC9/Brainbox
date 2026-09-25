import tempfile

from django.test import TestCase, override_settings

from apps.accounts.models import User
from apps.documents.models import DocumentStatus
from apps.documents.services import DocumentService
from apps.embeddings.services import IndexingService
from apps.search.services import SearchService
from apps.workspaces.services import WorkspaceService


@override_settings(KNOWLEDGE_DATA_ROOT=tempfile.mkdtemp())
class SearchServiceTests(TestCase):
    def setUp(self):
        self.alice = User.objects.create_user("alice", "alice@example.com", "pw")
        self.bob = User.objects.create_user("bob", "bob@example.com", "pw")
        self.workspace = WorkspaceService.create(name="Company", created_by=self.alice)
        self.approved = DocumentService.create(
            workspace=self.workspace,
            title="Azure Bicep deployment",
            path="azure.md",
            content="# Azure Bicep deployment\n\nUse bicep modules for container apps.\n",
            status=DocumentStatus.APPROVED,
            priority=100,
            created_by=self.alice,
        )
        self.draft = DocumentService.create(
            workspace=self.workspace,
            title="Azure draft idea",
            path="draft.md",
            content="# Azure draft idea\n\nMaybe use bicep someday.\n",
            status=DocumentStatus.DRAFT,
            created_by=self.alice,
        )
        IndexingService.index_document(self.approved, force=True)
        IndexingService.index_document(self.draft, force=True)

    def test_text_search_returns_matching_document(self):
        results = SearchService.search(self.alice, "bicep", mode="text")
        ids = {row["document_id"] for row in results}
        self.assertIn(str(self.approved.pk), ids)

    def test_semantic_search_finds_document(self):
        results = SearchService.search(self.alice, "container apps deployment", mode="semantic")
        self.assertTrue(results)

    def test_approved_knowledge_ranks_above_draft(self):
        results = SearchService.search(self.alice, "azure", mode="hybrid")
        self.assertEqual(results[0]["document_id"], str(self.approved.pk))

    def test_results_are_permission_filtered(self):
        results = SearchService.search(self.bob, "bicep", mode="text")
        self.assertEqual(results, [])
