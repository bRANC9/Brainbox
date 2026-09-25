import tempfile

from django.test import TestCase, override_settings

from apps.accounts.models import User
from apps.documents.frontmatter import parse_frontmatter
from apps.documents.models import DocumentStatus
from apps.documents.services import DocumentService
from apps.links.models import ResourceLink
from apps.workspaces.services import ProjectService, WorkspaceService


@override_settings(KNOWLEDGE_DATA_ROOT=tempfile.mkdtemp())
class DocumentServiceTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user("alice", "alice@example.com", "pw")
        self.workspace = WorkspaceService.create(name="Company", created_by=self.user)

    def test_parse_frontmatter(self):
        text = "---\ntype: skill\nstatus: approved\npriority: 100\n---\n# Body\n"
        metadata, body = parse_frontmatter(text)
        self.assertEqual(metadata["type"], "skill")
        self.assertEqual(metadata["status"], "approved")
        self.assertIn("# Body", body)

    def test_create_reads_frontmatter_into_metadata(self):
        content = (
            "---\n"
            "title: Azure Bicep\n"
            "type: skill\n"
            "status: approved\n"
            "priority: 100\n"
            "tags: [azure, bicep]\n"
            "---\n"
            "# Azure Bicep\n\nContent here.\n"
        )
        document = DocumentService.create(
            workspace=self.workspace, content=content, path="skills/azure-bicep/SKILL.md"
        )

        self.assertEqual(document.title, "Azure Bicep")
        self.assertEqual(document.status, DocumentStatus.APPROVED)
        self.assertEqual(document.priority, 100)
        self.assertEqual(document.metadata["type"], "skill")
        self.assertEqual(document.current_version, 1)
        self.assertIn("Content here", DocumentService.read_content(document))

    def test_wikilinks_become_resource_links(self):
        first = DocumentService.create(
            workspace=self.workspace,
            title="First",
            content="See [[Second]] for details.",
            path="first.md",
            created_by=self.user,
        )
        second = DocumentService.create(
            workspace=self.workspace,
            title="Second",
            content="# Second\n",
            path="second.md",
            created_by=self.user,
        )
        link = ResourceLink.objects.filter(
            source=first.resource, target=second.resource
        ).first()
        self.assertIsNotNone(link)
        self.assertEqual(link.link_type, "wikilink")

    def test_update_creates_new_version_and_keeps_history(self):
        project = ProjectService.create(workspace=self.workspace, name="Azure", created_by=self.user)
        document = DocumentService.create(
            workspace=self.workspace, project=project, title="Deploy", content="v1", path="deploy.md"
        )
        DocumentService.update_content(document=document, content="v2", user=self.user)
        document.refresh_from_db()

        self.assertEqual(document.current_version, 2)
        self.assertEqual(document.versions.count(), 2)
        self.assertEqual(DocumentService.read_content(document), "v2")

        DocumentService.restore(document=document, version_number=1, user=self.user)
        document.refresh_from_db()
        self.assertEqual(DocumentService.read_content(document), "v1")
        self.assertEqual(document.current_version, 3)
