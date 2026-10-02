"""Workspace rename and re-slug: one service, three surfaces.

Renaming used to be possible only through a raw REST PATCH, which bypassed both
the ACL and the audit trail - and was impossible from the UI or MCP. Now the web
form, the REST endpoint and the MCP tool all call
:meth:`WorkspaceService.update`, so there is one rule and one audit entry.

The slug is a separate question: it is the URL key only (the on-disk layout
keys on the workspace UUID), so it is changeable - but it breaks links shared
under the old one, which the audit entry flags.
"""

from django.core.exceptions import PermissionDenied, ValidationError
from django.test import TestCase
from django.urls import reverse

from apps.accounts.models import User
from apps.audit.models import AuditAction, AuditEvent
from apps.permissions.constants import Permission, SubjectType
from apps.permissions.services import PermissionService
from apps.workspaces.services import WorkspaceService


class _WorkspaceCase(TestCase):
    def setUp(self):
        self.alice = User.objects.create_user("alice", "alice@example.com", "pw")
        self.writer = User.objects.create_user("writer", "writer@example.com", "pw")
        self.outsider = User.objects.create_user("outsider", "out@example.com", "pw")
        self.workspace = WorkspaceService.create(name="Personal", created_by=self.alice)
        PermissionService.grant(
            self.workspace.resource,
            subject_type=SubjectType.USER,
            subject_id=self.writer.id,
            permission=Permission.WRITE,
            created_by=self.alice,
        )


class WorkspaceRenameTests(_WorkspaceCase):
    def test_the_owner_can_rename(self):
        WorkspaceService.update(workspace=self.workspace, name="Tervek", actor=self.alice)
        self.workspace.refresh_from_db()
        self.assertEqual(self.workspace.name, "Tervek")
        self.assertEqual(self.workspace.slug, "personal", "the slug is not touched")

    def test_a_writer_cannot_rename(self):
        with self.assertRaises(PermissionDenied):
            WorkspaceService.update(workspace=self.workspace, name="Tervek", actor=self.writer)
        self.workspace.refresh_from_db()
        self.assertEqual(self.workspace.name, "Personal")

    def test_a_no_op_rename_writes_nothing(self):
        before = AuditEvent.objects.filter(detail__type="workspace_update").count()
        WorkspaceService.update(workspace=self.workspace, name="Personal", actor=self.alice)
        self.assertEqual(
            AuditEvent.objects.filter(detail__type="workspace_update").count(), before
        )

    def test_a_rename_is_audited_with_the_previous_value(self):
        WorkspaceService.update(workspace=self.workspace, name="Tervek", actor=self.alice)
        event = AuditEvent.objects.filter(detail__type="workspace_update").latest("id")
        self.assertEqual(event.detail["changed"]["name"], "Personal")
        self.assertFalse(event.detail["broke_links"])
        self.assertEqual(event.action, AuditAction.UPDATE)

    def test_the_web_form_renames(self):
        self.client.force_login(self.alice)
        response = self.client.post(
            reverse("web:workspace_rename", args=[self.workspace.slug]),
            {"name": "Tervek", "description": "tervdokumentumok"},
            follow=True,
        )
        self.assertEqual(response.status_code, 200)
        self.workspace.refresh_from_db()
        self.assertEqual(self.workspace.name, "Tervek")

    def test_the_web_form_is_refused_without_admin(self):
        self.client.force_login(self.writer)
        self.client.post(
            reverse("web:workspace_rename", args=[self.workspace.slug]),
            {"name": "Tervek"},
        )
        self.workspace.refresh_from_db()
        self.assertEqual(self.workspace.name, "Personal", "a writer must not rename it")

    def test_the_owner_sees_the_form_and_a_writer_does_not(self):
        self.client.force_login(self.alice)
        response = self.client.get(
            reverse("web:workspace_detail", args=[self.workspace.slug])
        )
        self.assertContains(response, "Beállítások")
        self.assertContains(response, 'value="Personal"')

        self.client.force_login(self.writer)
        response = self.client.get(
            reverse("web:workspace_detail", args=[self.workspace.slug])
        )
        self.assertNotContains(response, 'name="description"')

    def test_a_non_reader_gets_a_404(self):
        self.client.force_login(self.outsider)
        response = self.client.get(
            reverse("web:workspace_detail", args=[self.workspace.slug])
        )
        self.assertEqual(response.status_code, 404)

    def test_the_rest_patch_renames_and_audits(self):
        from rest_framework.test import APIClient

        client = APIClient()
        client.force_authenticate(self.alice)
        response = client.patch(
            f"/api/v1/workspaces/{self.workspace.pk}/", {"name": "Tervek"}
        )
        self.assertEqual(response.status_code, 200)
        self.workspace.refresh_from_db()
        self.assertEqual(self.workspace.name, "Tervek")
        self.assertTrue(
            AuditEvent.objects.filter(detail__type="workspace_update").exists(),
            "a REST rename must not be an unaudited write",
        )

    def test_the_rest_patch_is_refused_for_a_writer(self):
        from rest_framework.test import APIClient

        client = APIClient()
        client.force_authenticate(self.writer)
        response = client.patch(
            f"/api/v1/workspaces/{self.workspace.pk}/", {"name": "Tervek"}
        )
        self.assertEqual(response.status_code, 403)
        self.workspace.refresh_from_db()
        self.assertEqual(self.workspace.name, "Personal")


class WorkspaceSlugTests(_WorkspaceCase):
    def test_the_slug_can_be_changed_and_is_normalised(self):
        WorkspaceService.update(
            workspace=self.workspace, slug="Brain Box!", actor=self.alice
        )
        self.workspace.refresh_from_db()
        self.assertEqual(self.workspace.slug, "brain-box")

    def test_a_slug_change_is_audited_as_breaking_links(self):
        WorkspaceService.update(workspace=self.workspace, slug="brainbox", actor=self.alice)
        event = AuditEvent.objects.filter(detail__type="workspace_update").latest("id")
        self.assertEqual(event.detail["changed"]["slug"], "personal")
        self.assertTrue(event.detail["broke_links"])

    def test_a_taken_slug_is_refused(self):
        WorkspaceService.create(name="Ecoform", created_by=self.alice)
        with self.assertRaises(ValidationError):
            WorkspaceService.update(workspace=self.workspace, slug="ecoform", actor=self.alice)
        self.workspace.refresh_from_db()
        self.assertEqual(self.workspace.slug, "personal")

    def test_an_empty_slug_leaves_it_alone(self):
        WorkspaceService.update(workspace=self.workspace, slug="   ", actor=self.alice)
        self.workspace.refresh_from_db()
        self.assertEqual(self.workspace.slug, "personal")

    def test_a_writer_cannot_change_the_slug(self):
        with self.assertRaises(PermissionDenied):
            WorkspaceService.update(workspace=self.workspace, slug="brainbox", actor=self.writer)
        self.workspace.refresh_from_db()
        self.assertEqual(self.workspace.slug, "personal")

    def test_the_web_form_changes_the_slug_and_redirects_to_the_new_url(self):
        self.client.force_login(self.alice)
        response = self.client.post(
            reverse("web:workspace_rename", args=[self.workspace.slug]),
            {"name": "BrainBox", "slug": "brainbox"},
        )
        self.assertEqual(response.status_code, 302)
        self.assertTrue(response.url.endswith("/workspaces/brainbox/"))
        self.workspace.refresh_from_db()
        self.assertEqual(self.workspace.slug, "brainbox")

    def test_the_web_form_reports_a_taken_slug(self):
        WorkspaceService.create(name="Ecoform", created_by=self.alice)
        self.client.force_login(self.alice)
        response = self.client.post(
            reverse("web:workspace_rename", args=[self.workspace.slug]),
            {"slug": "ecoform"},
            follow=True,
        )
        self.assertContains(response, "már foglalt")
        self.workspace.refresh_from_db()
        self.assertEqual(self.workspace.slug, "personal")

    def test_the_rest_patch_changes_the_slug(self):
        from rest_framework.test import APIClient

        client = APIClient()
        client.force_authenticate(self.alice)
        response = client.patch(
            f"/api/v1/workspaces/{self.workspace.pk}/", {"slug": "brainbox"}
        )
        self.assertEqual(response.status_code, 200)
        self.workspace.refresh_from_db()
        self.assertEqual(self.workspace.slug, "brainbox")

    def test_the_rest_patch_reports_a_taken_slug_as_a_400(self):
        WorkspaceService.create(name="Ecoform", created_by=self.alice)
        from rest_framework.test import APIClient

        client = APIClient()
        client.force_authenticate(self.alice)
        response = client.patch(
            f"/api/v1/workspaces/{self.workspace.pk}/", {"slug": "ecoform"}
        )
        self.assertEqual(response.status_code, 400)

    def test_the_slug_field_is_in_the_form_with_a_warning(self):
        self.client.force_login(self.alice)
        response = self.client.get(
            reverse("web:workspace_detail", args=[self.workspace.slug])
        )
        self.assertContains(response, 'name="slug"')
        self.assertContains(response, "megtöri a régi linkeket")