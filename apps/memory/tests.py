import tempfile

from django.test import TestCase, override_settings

from apps.accounts.models import User
from apps.documents.services import DocumentService
from apps.permissions.constants import Permission, SubjectType
from apps.permissions.services import PermissionService
from apps.workspaces.services import WorkspaceService

from .models import Fact, MemoryAggregate
from .services import AggregateService, FactService, ReflectionService


@override_settings(KNOWLEDGE_DATA_ROOT=tempfile.mkdtemp())
class FactTests(TestCase):
    def setUp(self):
        self.alice = User.objects.create_user("alice", "alice@example.com", "pw")
        self.bob = User.objects.create_user("bob", "bob@example.com", "pw")
        self.workspace = WorkspaceService.create(name="Ecoform", created_by=self.alice)
        self.hr = DocumentService.create(
            workspace=self.workspace,
            title="Payroll",
            path="hr/payroll.md",
            content="---\nowner: Kovács Anna\n---\n# Payroll\n",
            created_by=self.alice,
        )
        FactService.extract_document(self.hr)

    def test_extraction_creates_a_cited_fact(self):
        fact = Fact.objects.get(predicate="owner_of")
        self.assertEqual(fact.subject, "Kovács Anna")
        self.assertEqual(fact.object, "Payroll")
        self.assertEqual(fact.source_document_id, self.hr.pk)

    def test_the_view_groups_by_subject_and_cites_the_source(self):
        view = FactService.view(self.alice)
        subjects = {row["subject"]: row for row in view["subjects"]}
        self.assertIn("Kovács Anna", subjects)
        row = subjects["Kovács Anna"]["facts"][0]
        self.assertEqual(row["source"]["document_id"], str(self.hr.pk))
        self.assertEqual(row["source"]["tree_path"], "hr/payroll.md")

    def test_a_fact_is_never_wider_than_its_source(self):
        # Bob cannot read the HR document, so the fact about Anna is absent.
        self.assertEqual(FactService.view(self.bob)["subjects"], [])
        # Granting read on the *document* makes exactly that fact appear.
        PermissionService.grant(
            self.hr.resource,
            subject_type=SubjectType.USER,
            subject_id=self.bob.id,
            permission=Permission.READ,
            created_by=self.alice,
        )
        subjects = {row["subject"] for row in FactService.view(self.bob)["subjects"]}
        self.assertIn("Kovács Anna", subjects)

    def test_llm_extraction_parses_triples(self):
        from unittest.mock import patch

        triples = '[{"subject": "Anna", "predicate": "manages", "object": "Payroll"}]'
        with patch("apps.knowledge.llm.get_llm_provider") as provider:
            provider.return_value.generate.return_value = triples
            created = FactService.extract_document_llm(self.hr)
        self.assertEqual(created, 1)
        self.assertEqual(Fact.objects.get(origin="llm").predicate, "manages")

    def test_an_aggregate_needs_every_source_readable(self):
        second = DocumentService.create(
            workspace=self.workspace, title="Roles", path="roles.md",
            content="---\nowner: Kovács Anna\n---\n# Roles\n", created_by=self.alice,
        )
        FactService.extract_document(second)
        AggregateService.build_workspace(self.workspace)
        aggregate = MemoryAggregate.objects.get(subject_key="kovács anna")
        self.assertEqual(aggregate.sources.count(), 2)

        # Alice sees the aggregate; bob, who can read neither source, does not.
        self.assertTrue(
            any(g["aggregates"] for g in FactService.view(self.alice)["subjects"])
        )
        self.assertFalse(
            any(g.get("aggregates") for g in FactService.view(self.bob)["subjects"])
        )
        # Read on one source only: still hidden - the intersection.
        PermissionService.grant(
            second.resource, subject_type=SubjectType.USER, subject_id=self.bob.id,
            permission=Permission.READ, created_by=self.alice,
        )
        self.assertFalse(
            any(g.get("aggregates") for g in FactService.view(self.bob)["subjects"])
        )
        # Read on both: now it appears.
        PermissionService.grant(
            self.hr.resource, subject_type=SubjectType.USER, subject_id=self.bob.id,
            permission=Permission.READ, created_by=self.alice,
        )
        self.assertTrue(
            any(g.get("aggregates") for g in FactService.view(self.bob)["subjects"])
        )

    def test_reflection_parses_the_mcp_answer(self):
        from unittest.mock import patch

        from apps.gateway.services import GatewayService

        target = GatewayService.create(
            name="hindsight", base_url="https://hindsight.example.com",
            workspace=self.workspace, created_by=self.alice,
        )
        sse = (
            'data: {"jsonrpc": "2.0", "id": 1, "result": '
            '{"content": [{"type": "text", "text": "Anna owns Payroll."}]}}\n\n'
        )
        with patch.object(
            GatewayService, "_send",
            lambda *a, **k: (200, {"Content-Type": "text/event-stream"}, sse, False),
        ):
            text = ReflectionService.reflect(
                target=target, bank="anna", query="?", user=self.alice
            )
        self.assertEqual(text, "Anna owns Payroll.")
