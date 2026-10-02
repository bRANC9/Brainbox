"""The three follow-ups from the access-model review.

Each of these was a real hole rather than a missing feature:

* the graph walk emitted the *name* of a neighbour the caller may not read, and
  the fix only covered one of the three surfaces that share the walk;
* a document could publish its title to non-readers by accident, because any
  truthy ``public_summary`` in a frontmatter block was enough, and the read was
  not audited;
* deleting a user who owns shared content raised ``ProtectedError``, i.e. a 500.
"""

from django.test import TestCase

from apps.accounts.models import User
from apps.accounts.services import ApiKeyService, OwnerConflict, UserService
from apps.audit.models import AuditEvent
from apps.documents.services import DocumentService
from apps.knowledge.services import GraphService, publishes_summary
from apps.links.models import ResourceLink
from apps.permissions.constants import Permission, SubjectType
from apps.permissions.services import PermissionService
from apps.workspaces.models import Workspace
from apps.workspaces.personal import PersonalWorkspaceService
from apps.workspaces.services import ProjectService, WorkspaceService


class GraphLeakTests(TestCase):
    """One implementation, three surfaces - so the filter has to live in it."""

    def setUp(self):
        self.alice = User.objects.create_user("alice", "alice@example.com", "pw")
        self.bob = User.objects.create_user("bob", "bob@example.com", "pw")
        self.workspace = WorkspaceService.create(name="Company", created_by=self.alice)
        self.project = ProjectService.create(
            workspace=self.workspace, name="Ops", created_by=self.alice
        )
        self.public = DocumentService.create(
            workspace=self.workspace,
            project=self.project,
            title="Runbook",
            content="# x",
            path="runbook.md",
            created_by=self.alice,
        )
        self.secret = DocumentService.create(
            workspace=self.workspace,
            project=self.project,
            title="Salary",
            content="# x",
            path="salary.md",
            created_by=self.alice,
        )
        ResourceLink.objects.create(
            source=self.public.resource, target=self.secret.resource, link_type="related"
        )
        PermissionService.grant(
            self.public.resource,
            subject_type=SubjectType.USER,
            subject_id=self.bob.id,
            permission=Permission.READ,
            created_by=self.alice,
        )

    def test_an_unreadable_neighbour_is_omitted_entirely(self):
        rows = GraphService.neighbors(self.bob, self.public.resource)
        self.assertEqual(rows, [], "the row itself is the leak: it carries the name")

    def test_the_walk_does_not_traverse_through_an_unreadable_node(self):
        deeper = DocumentService.create(
            workspace=self.workspace,
            project=self.project,
            title="Deeper secret",
            content="# x",
            path="deeper.md",
            created_by=self.alice,
        )
        ResourceLink.objects.create(
            source=self.secret.resource, target=deeper.resource, link_type="related"
        )
        # depth=2 could only reach the third document by passing through the one
        # bob may not read, so nothing may come back at all.
        self.assertEqual(GraphService.neighbors(self.bob, self.public.resource, depth=2), [])

    def test_the_inaccessible_rows_are_an_explicit_opt_in(self):
        rows = GraphService.neighbors(
            self.bob, self.public.resource, include_inaccessible=True
        )
        self.assertEqual([row["name"] for row in rows], ["Salary"])
        self.assertFalse(rows[0]["accessible"])

    def test_the_owner_sees_everything(self):
        rows = GraphService.neighbors(self.alice, self.public.resource)
        self.assertEqual([row["name"] for row in rows], ["Salary"])


class PublishedSummaryTests(TestCase):
    """A publication has to be a deliberate, audited act - not a truthy string."""

    def setUp(self):
        self.alice = User.objects.create_user("alice", "alice@example.com", "pw")
        self.bob = User.objects.create_user("bob", "bob@example.com", "pw")
        self.workspace = WorkspaceService.create(name="Company", created_by=self.alice)
        self.project = ProjectService.create(
            workspace=self.workspace, name="Ops", created_by=self.alice
        )
        self.document = DocumentService.create(
            workspace=self.workspace,
            project=self.project,
            title="Handbook",
            content="# x",
            summary="The one everybody needs.",
            path="handbook.md",
            created_by=self.alice,
        )

    def _enable_workspace_publication(self):
        metadata = dict(self.workspace.resource.metadata or {})
        metadata["publish_titles"] = True
        self.workspace.resource.metadata = metadata
        self.workspace.resource.save(update_fields=["metadata"])

    def test_a_stray_truthy_value_does_not_publish(self):
        """The accident this guards: a leftover key in a template or frontmatter."""
        metadata = dict(self.document.resource.metadata or {})
        metadata["public_summary"] = "yes please"
        self.document.resource.metadata = metadata
        self.document.resource.save(update_fields=["metadata"])
        self._enable_workspace_publication()
        self.assertFalse(publishes_summary(self.document.resource))

    def test_publishing_needs_the_workspace_switch_too(self):
        DocumentService.set_published_summary(self.document, True, user=self.alice)
        self.assertTrue(
            (self.document.resource.metadata or {}).get("public_summary") is True
        )
        self.assertFalse(
            publishes_summary(self.document.resource),
            "a per-document flag alone must not publish anything",
        )
        self._enable_workspace_publication()
        self.assertTrue(publishes_summary(self.document.resource))

    def test_publishing_is_stored_as_a_boolean_and_removed_when_off(self):
        DocumentService.set_published_summary(self.document, True, user=self.alice)
        DocumentService.set_published_summary(self.document, False, user=self.alice)
        self.document.resource.refresh_from_db()
        self.assertNotIn("public_summary", self.document.resource.metadata or {})

    def test_every_publish_toggle_is_audited(self):
        DocumentService.set_published_summary(self.document, True, user=self.alice)
        event = AuditEvent.objects.filter(detail__type="published_summary").latest("id")
        self.assertTrue(event.detail["published"])

    def test_a_published_read_is_audited(self):
        """The disclosure is the point - so it has to leave a trace."""
        from apps.mcp.tools import ToolContext, ToolError, tool_get_summary

        ctx = ToolContext(user=self.bob, api_key=None, request=None)
        args = {"document_id": str(self.document.pk)}

        # Nothing published: denied, and nothing in the log.
        with self.assertRaises(ToolError):
            tool_get_summary(ctx, args)
        self.assertFalse(AuditEvent.objects.filter(detail__via="public_summary").exists())

        DocumentService.set_published_summary(self.document, True, user=self.alice)
        # Flag set but the workspace switch is off: still denied.
        with self.assertRaises(ToolError):
            tool_get_summary(ctx, args)

        self._enable_workspace_publication()
        result = tool_get_summary(ctx, args)
        self.assertEqual(result["access"], "summary")
        self.assertEqual(result["title"], "Handbook")
        event = AuditEvent.objects.filter(detail__via="public_summary").latest("id")
        self.assertEqual(event.user_id, self.bob.id)
        self.assertEqual(event.detail["via"], "public_summary")
        self.assertEqual(str(event.resource_id), str(self.document.resource_id))

        # A reader still gets the full thing, and no publication event.
        before = AuditEvent.objects.filter(detail__via="public_summary").count()
        owner_ctx = ToolContext(user=self.alice, api_key=None, request=None)
        self.assertEqual(tool_get_summary(owner_ctx, args)["access"], "full")
        self.assertEqual(
            AuditEvent.objects.filter(detail__via="public_summary").count(), before
        )

    def test_a_personal_workspace_never_offers_publication(self):
        from django.test import RequestFactory

        from apps.web.views import _can_publish_titles

        mine = PersonalWorkspaceService.get_or_create(self.alice)
        request = RequestFactory().get("/")
        request.user = self.alice
        self.assertFalse(_can_publish_titles(self.alice, mine))


class UserDeletionTests(TestCase):
    """Deletion is the wrong operation; deactivation is the right one."""

    def setUp(self):
        self.root = User.objects.create_user("root", "root@example.com", "pw")
        self.root.is_superuser = True
        self.root.is_staff = True
        self.root.save()
        self.alice = User.objects.create_user("alice", "alice@example.com", "pw")
        self.bob = User.objects.create_user("bob", "bob@example.com", "pw")
        self.workspace = WorkspaceService.create(name="Ecoform", created_by=self.alice)
        self.project = ProjectService.create(
            workspace=self.workspace, name="Deploy", created_by=self.alice
        )

    def test_a_user_who_owns_shared_content_cannot_be_deleted(self):
        with self.assertRaises(OwnerConflict) as caught:
            UserService.delete(user=self.alice, actor=self.root)
        detail = caught.exception.detail
        self.assertEqual([ws["slug"] for ws in detail["owned_workspaces"]], ["ecoform"])
        self.assertIn("hint", detail)
        self.assertTrue(User.objects.filter(pk=self.alice.pk).exists())

    def test_the_conflict_lists_a_owned_project_too(self):
        Ownership = ProjectService.create(
            workspace=self.workspace, name="Személyes", created_by=self.bob
        )
        self.assertTrue(Ownership.pk)
        with self.assertRaises(OwnerConflict) as caught:
            UserService.delete(user=self.bob, actor=self.root)
        self.assertEqual(
            [pr["name"] for pr in caught.exception.detail["owned_projects"]], ["Személyes"]
        )

    def test_a_personal_workspace_does_not_block_deletion(self):
        PersonalWorkspaceService.get_or_create(self.bob)
        self.assertTrue(UserService.can_delete(self.bob))
        UserService.delete(user=self.bob, actor=self.root)
        self.assertFalse(User.objects.filter(pk=self.bob.pk).exists())

    def test_deleting_a_user_takes_their_personal_workspace_with_them(self):
        mine = PersonalWorkspaceService.get_or_create(self.bob)
        document = DocumentService.create(
            workspace=mine,
            title="Jegyzet",
            content="# x",
            path="note.md",
            created_by=self.bob,
        )
        from apps.documents.models import Document
        from apps.workspaces.models import Workspace

        UserService.delete(user=self.bob, actor=self.root)
        self.assertFalse(Workspace.objects.filter(pk=mine.pk).exists())
        self.assertFalse(Document.objects.filter(pk=document.pk).exists())

    def test_deactivation_keeps_the_rows_and_drops_the_power(self):
        key, _ = ApiKeyService.create(user=self.alice, name="ci", actor=self.root)
        UserService.deactivate(user=self.alice, actor=self.root)
        self.alice.refresh_from_db()
        key.refresh_from_db()
        self.assertFalse(self.alice.is_active)
        self.assertFalse(self.alice.is_superuser)
        self.assertFalse(self.alice.is_staff)
        self.assertTrue(User.objects.filter(pk=self.alice.pk).exists())
        self.assertTrue(
            Workspace.objects.filter(pk=self.workspace.pk).exists(),
            "deactivating must never take content with it",
        )
        self.assertTrue(key.revoked_at is not None, "a deactivated user's keys stop working")

    def test_deactivating_a_superuser_removes_superuser(self):
        UserService.deactivate(user=self.root, actor=self.root)
        self.root.refresh_from_db()
        self.assertFalse(self.root.is_superuser)

    def test_the_rest_api_answers_409_instead_of_500(self):
        from rest_framework.test import APIClient

        client = APIClient()
        client.force_authenticate(self.root)
        response = client.delete(f"/api/v1/users/{self.alice.pk}/")
        self.assertEqual(response.status_code, 409)
        body = response.json()
        self.assertEqual(body["owned_workspaces"][0]["slug"], "ecoform")
        self.assertIn("hint", body)

    def test_the_rest_api_reports_ownership(self):
        from rest_framework.test import APIClient

        client = APIClient()
        client.force_authenticate(self.root)
        response = client.get(f"/api/v1/users/{self.alice.pk}/ownership/")
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertFalse(body["can_delete"])
        self.assertEqual(body["owned_workspaces"][0]["slug"], "ecoform")

    def test_the_rest_api_deletes_a_user_who_owns_nothing_shared(self):
        from rest_framework.test import APIClient

        PersonalWorkspaceService.get_or_create(self.bob)
        client = APIClient()
        client.force_authenticate(self.root)
        response = client.delete(f"/api/v1/users/{self.bob.pk}/")
        self.assertEqual(response.status_code, 204)
        self.assertFalse(User.objects.filter(pk=self.bob.pk).exists())

    def test_the_rest_api_can_deactivate(self):
        from rest_framework.test import APIClient

        client = APIClient()
        client.force_authenticate(self.root)
        response = client.post(f"/api/v1/users/{self.alice.pk}/deactivate/")
        self.assertEqual(response.status_code, 200)
        self.alice.refresh_from_db()
        self.assertFalse(self.alice.is_active)
        self.assertEqual(Workspace.objects.filter(pk=self.workspace.pk).count(), 1)

    def test_you_cannot_deactivate_yourself(self):
        from rest_framework.test import APIClient

        client = APIClient()
        client.force_authenticate(self.root)
        response = client.post(f"/api/v1/users/{self.root.pk}/deactivate/")
        self.assertEqual(response.status_code, 400)

    def test_the_django_admin_hides_delete_when_blocked(self):
        from django.contrib.admin.sites import AdminSite
        from django.test import RequestFactory

        from apps.accounts.admin import UserAdmin

        request = RequestFactory().get("/")
        request.user = self.root
        model_admin = UserAdmin(User, AdminSite())
        self.assertFalse(model_admin.has_delete_permission(request, self.alice))
        PersonalWorkspaceService.get_or_create(self.bob)
        self.assertTrue(model_admin.has_delete_permission(request, self.bob))
