from django.test import TestCase
from django.urls import reverse

from apps.accounts.models import ApiKey, User
from apps.permissions.constants import Permission
from apps.permissions.models import ResourceACL
from apps.permissions.services import PermissionService
from apps.workspaces.services import WorkspaceService


class ManagementUITests(TestCase):
    def setUp(self):
        self.alice = User.objects.create_user("alice", "alice@example.com", "pw")
        self.workspace = WorkspaceService.create(name="Company", created_by=self.alice)
        self.client.force_login(self.alice)

    def test_api_key_create_and_revoke(self):
        self.client.post(reverse("web:api_keys"), {"action": "create", "name": "laptop"})
        key = ApiKey.objects.get(user=self.alice, name="laptop")
        self.assertTrue(key.is_active)

        self.client.post(
            reverse("web:api_keys"), {"action": "revoke", "key_id": str(key.pk)}
        )
        key.refresh_from_db()
        self.assertFalse(key.is_active)

    def test_permission_grant_and_revoke(self):
        bob = User.objects.create_user("bob", "bob@example.com", "pw")
        url = reverse("web:resource_permissions", args=[self.workspace.resource_id])

        self.client.post(
            url,
            {
                "action": "grant",
                "subject_type": "user",
                "subject_id": "bob",
                "permission": "read",
                "effect": "allow",
                "inherit": "on",
            },
        )
        self.assertTrue(PermissionService.check(bob, self.workspace.resource, Permission.READ))

        self.client.post(
            url,
            {
                "action": "revoke",
                "subject_type": "user",
                "subject_id": str(bob.id),
                "permission": "read",
            },
        )
        self.assertFalse(PermissionService.check(bob, self.workspace.resource, Permission.READ))
        self.assertFalse(
            ResourceACL.objects.filter(resource=self.workspace.resource, subject_id=bob.id).exists()
        )

    def test_permission_page_forbidden_without_admin(self):
        bob = User.objects.create_user("bob2", "bob2@example.com", "pw")
        self.client.force_login(bob)
        response = self.client.get(
            reverse("web:resource_permissions", args=[self.workspace.resource_id])
        )
        self.assertEqual(response.status_code, 403)

    def test_audit_dashboard_renders(self):
        response = self.client.get(reverse("web:audit_dashboard"))
        self.assertEqual(response.status_code, 200)

    def test_groups_requires_staff(self):
        response = self.client.get(reverse("web:groups_admin"))
        self.assertEqual(response.status_code, 403)
