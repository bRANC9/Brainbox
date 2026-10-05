import tempfile

from django.test import TestCase, override_settings

from apps.accounts.models import User
from apps.documents.folders import create_folder
from apps.documents.models import DocumentFolder
from apps.documents.services import DocumentService
from apps.tags.services import (
    effective_tags,
    filter_documents_by_tag,
    tag_target,
    tags_for,
    untag_target,
)
from apps.workspaces.services import ProjectService, WorkspaceService


@override_settings(KNOWLEDGE_DATA_ROOT=tempfile.mkdtemp())
class TaggingTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user("alice", "alice@example.com", "pw")
        self.workspace = WorkspaceService.create(name="Ecoform", created_by=self.user)
        self.project = ProjectService.create(
            workspace=self.workspace, name="Deploy", created_by=self.user
        )

    def test_tag_and_untag_document(self):
        doc = DocumentService.create(
            workspace=self.workspace, project=self.project, title="A",
            content="# A\n", path="a.md", created_by=self.user,
        )
        tags = tag_target(doc, ["Azure", "Bicep", "azure"], user=self.user)
        self.assertEqual(tags, ["azure", "bicep"])  # normalized + deduped
        self.assertEqual(untag_target(doc, ["bicep"]), ["azure"])

    def test_folder_tags_apply_to_children(self):
        create_folder(workspace=self.workspace, project=self.project, path="dotnet", created_by=self.user)
        folder = DocumentFolder.objects.get(path="dotnet")
        tag_target(folder, ["stack:dotnet"], user=self.user)

        doc = DocumentService.create(
            workspace=self.workspace, project=self.project, title="Child",
            content="# C\n", path="dotnet/child.md", created_by=self.user,
        )
        self.assertIn("stack-dotnet", effective_tags(doc))

        docs = filter_documents_by_tag([doc], "stack-dotnet")
        self.assertEqual(len(docs), 1)
        docs = filter_documents_by_tag([doc], "nincs")
        self.assertEqual(len(docs), 0)

    def test_web_folder_tags_by_tree_path(self):
        create_folder(workspace=self.workspace, project=self.project, path="dotnet", created_by=self.user)
        self.client.force_login(self.user)
        response = self.client.post(
            "/folders/tags/",
            {
                "action": "add",
                "workspace": self.workspace.slug,
                "node": f"{self.project.name}/dotnet",
                "tags": "stack:dotnet",
            },
        )
        self.assertEqual(response.status_code, 302)
        folder = DocumentFolder.objects.get(name="dotnet")
        self.assertIn("stack-dotnet", tags_for(folder))

    def test_web_tag_and_filter(self):
        doc = DocumentService.create(
            workspace=self.workspace, project=self.project, title="Tagged",
            content="# T\n", path="tagged.md", created_by=self.user,
        )
        self.client.force_login(self.user)
        # add a tag via web endpoint
        r = self.client.post(f"/documents/{doc.pk}/tags/", {"action": "add", "tags": "azure"})
        self.assertEqual(r.status_code, 302)
        self.assertIn("azure", tags_for(doc))
        # filter the node's tree by tag (a project is a node now, addressed by
        # its tree path rather than a project slug)
        page = self.client.get(f"/workspaces/{self.workspace.slug}/f/{self.project.name}/?tag=azure")
        self.assertEqual(page.status_code, 200)
        self.assertEqual(len(page.context["tree_rows"]), 1)
