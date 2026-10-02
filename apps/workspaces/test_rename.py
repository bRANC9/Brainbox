"""Workspace rename: one service, three surfaces.

Renaming used to be possible only through a raw REST PATCH, which bypassed both
the ACL and the audit trail - and was impossible from the UI or MCP. Now the web
form, the REST endpoint and the MCP tool all call
:meth:`WorkspaceService.update`, so there is one rule and one audit entry.
"""

from django.core.exceptions import PermissionDenied
from django.test import TestCase
from django.urls import reverse

from apps.accounts.models import User
from apps.audit.models import AuditAction, AuditEvent
from apps.permissions.constants import Permission, SubjectType
from apps.permissions.services import PermissionService
from apps.workspaces.services import WorkspaceService


class WorkspaceRenameTests(TestCase):
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

    def test_the_owner_can_rename(self):
        WorkspaceService.update(workspace=self.workspace, name="Tervek", actor=self.alice)
        self.workspace.refresh_from_db()
        self.assertEqual(self.workspace.name, "Tervek")
        # The slug stays, because it is in every URL and in the on-disk layout.
        self.assertEqual(self.workspace.slug, "personal")

    def test_a_writer_cannot_rename(self):
        with self.assertRaises(PermissionDenied):
            WorkspaceService.update(workspace=self.workspace, name="Tervek", actor=self.writer)
        self.workspace.refresh_from_db()
        self.assertEqual(self.workspace.name, "Personal")

    def test_a_rename_is_audited_with_the_old_value(self):
        WorkspaceService.update(workspace=self.workspace, name="Tervek", actor=self.alice)
        event = AuditEvent.objects.filter(detail__type="workspace_rename").latest("id")
        self.assertEqual(event.detail["changed"]["name"], "Personal")
        self.assertEqual(event.action, AuditAction.UPDATE)

    def test_a_no_op_rename_writes_nothing(self):
        before = AuditEvent.objects.filter(detail__type="workspace_rename").count()
        WorkspaceService.update(workspace=self.workspace, name="Personal", actor=self.alice)
        self.assertEqual(
            AuditEvent.objects.filter(detail__type="workspace_rename").count(), before
        )

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

    def test_the_web_form_is_hidden_and_refused_without_admin(self):
        self.client.force_login(self.writer)
        self.client.post(
            reverse("web:workspace_rename", args=[self.workspace.slug]),
            {"name": "Tervek"},
        )
        self.workspace.refresh_from_db()
        self.assertEqual(self.workspace.name, "Personal", "a writer must not rename it")
        response = self.client.get(reverse("web:workspace_detail", args=[self.workspace.slug]))
        self.assertNotContains(response, 'name="workspace_rename"')
        self.assertNotContains(response, "Beállítások")

    def test_the_owner_sees_the_form(self):
        self.client.force_login(self.alice)
        response = self.client.get(reverse("web:workspace_detail", args=[self.workspace.slug]))
        self.assertContains(response, "Beállítások")
        self.assertContains(response, 'value="Personal"')

    def test_a_non_reader_gets_a_404_not_a_rename_form(self):
        self.client.force_login(self.outsider)
        response = self.client.get(reverse("web:workspace_detail", args=[self.workspace.slug]))
        self.assertEqual(response.status_code, 404)

    def test_the_rest_patch_renames(self):
        from rest_framework.test import APIClient

        client = APIClient()
        client.force_authenticate(self.alice)
        response = client.patch(f"/api/v1/workspaces/{self.workspace.pk}/", {"name": "Tervek"})
        self.assertEqual(response.status_code, 200)
        self.workspace.refresh_from_db()
        self.assertEqual(self.workspace.name, "Tervek")
        self.assertTrue(
            AuditEvent.objects.filter(detail__type="workspace_rename").exists(),
            "a REST rename must not be an unaudited write",
        )

    def test_the_rest_patch_ignores_an_attempted_slug_change(self):
        from rest_framework.test import APIClient

        client = APIClient()
        client.force_authenticate(self.alice)
        client.patch(
            f"/api/v1/workspaces/{self.workspace.pk}/",
            {"name": "Tervek", "slug": "atirhato"},
        )
        self.workspace.refresh_from_db()
        self.assertEqual(self.workspace.slug, "personal")

    def test_the_rest_patch_is_refused_for_a_writer(self):
        from rest_framework.test import APIClient

        client = APIClient()
        client.force_authenticate(self.writer)
        response = client.patch(f"/api/v1/workspaces/{self.workspace.pk}/", {"name": "Tervek"})
        self.assertEqual(response.status_code, 403)
        self.workspace.refresh_from_db()
        self.assertEqual(self.workspace.name, "Personal")