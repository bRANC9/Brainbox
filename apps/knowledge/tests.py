import tempfile

from django.test import TestCase, override_settings

from apps.accounts.models import User
from apps.documents.models import DocumentStatus
from apps.documents.services import DocumentService
from apps.knowledge.services import DiscoveryService, DraftService, GraphService, QualityService
from apps.links.services import LinkService
from apps.workspaces.services import WorkspaceService


@override_settings(KNOWLEDGE_DATA_ROOT=tempfile.mkdtemp())
class KnowledgeTests(TestCase):
    def setUp(self):
        self.alice = User.objects.create_user("alice", "alice@example.com", "pw")
        self.workspace = WorkspaceService.create(name="Company", created_by=self.alice)
        self.skill = DocumentService.create(
            workspace=self.workspace,
            title="Azure bicep skill",
            path="skills/azure-bicep/SKILL.md",
            content="---\ntype: skill\n---\n# Azure bicep skill\n",
            created_by=self.alice,
        )
        self.note = DocumentService.create(
            workspace=self.workspace,
            title="Deployment notes",
            path="notes/deploy.md",
            content="# Deployment notes\n",
            created_by=self.alice,
        )

    def test_discovery_groups_knowledge_by_type(self):
        buckets = DiscoveryService.discover(self.alice)
        self.assertIn("Azure bicep skill", [row["title"] for row in buckets["skill"]])

    def test_graph_related_is_link_based_and_permission_filtered(self):
        LinkService.create(
            source=self.skill.resource, target=self.note.resource, created_by=self.alice
        )
        related = GraphService.neighbors(self.alice, self.skill.resource, depth=1)
        self.assertEqual(related[0]["resource_id"], str(self.note.resource_id))
        self.assertTrue(related[0]["accessible"])

    def test_draft_generation_and_approval(self):
        draft = DraftService.generate_draft(
            workspace=self.workspace,
            title="Draft idea",
            prompt="how do we deploy",
            user=self.alice,
        )
        self.assertEqual(draft.status, DocumentStatus.DRAFT)
        self.assertTrue(draft.metadata.get("ai_generated"))

        approved = DraftService.set_status(
            document=draft, status=DocumentStatus.APPROVED, user=self.alice
        )
        self.assertEqual(approved.status, DocumentStatus.APPROVED)

    def test_quality_metrics(self):
        metrics = QualityService.metrics()
        self.assertGreaterEqual(metrics["documents"], 2)
        self.assertIn("draft", metrics["by_status"])
        self.assertEqual(len(metrics["orphans"]), 2)
