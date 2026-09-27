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


class AccessPanelTests(TestCase):
    def setUp(self):
        self.alice = User.objects.create_user("alice", "alice@example.com", "pw")
        self.writer = User.objects.create_user("writer", "writer@example.com", "pw")
        self.bob = User.objects.create_user("bob", "bob@example.com", "pw")
        self.workspace = WorkspaceService.create(name="Company", created_by=self.alice)
        # writer gets write (not admin) on the workspace
        from apps.permissions.constants import Permission, SubjectType
        from apps.permissions.services import PermissionService

        PermissionService.grant(
            self.workspace.resource,
            subject_type=SubjectType.USER,
            subject_id=self.writer.id,
            permission=Permission.WRITE,
        )

    def test_writer_can_open_access_panel(self):
        self.client.force_login(self.writer)
        response = self.client.get(reverse("web:resource_permissions", args=[self.workspace.resource_id]))
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.context["can_share"])
        self.assertFalse(response.context["can_admin"])

    def test_writer_can_share_read_but_not_write(self):
        from apps.permissions.constants import Effect, Permission
        from apps.permissions.services import PermissionService

        self.client.force_login(self.writer)
        url = reverse("web:resource_permissions", args=[self.workspace.resource_id])
        self.client.post(
            url,
            {"action": "grant", "subject_id": f"user:{self.bob.id}", "permission": "read", "effect": "allow", "inherit": "on"},
            follow=True,
        )
        self.assertTrue(PermissionService.check(self.bob, self.workspace.resource, Permission.READ))

        # writer tries to escalate to write -> rejected
        self.client.post(
            url,
            {"action": "grant", "subject_id": f"user:{self.bob.id}", "permission": "write", "effect": "allow", "inherit": "on"},
            follow=True,
        )
        self.assertFalse(PermissionService.check(self.bob, self.workspace.resource, Permission.WRITE))
        # deny is admin-only
        self.client.post(
            url,
            {"action": "grant", "subject_id": f"user:{self.bob.id}", "permission": "read", "effect": "deny"},
            follow=True,
        )
        # a deny from a writer must not have created a deny entry
        from apps.permissions.models import ResourceACL

        self.assertFalse(
            ResourceACL.objects.filter(
                resource=self.workspace.resource, subject_id=self.bob.id, effect=Effect.DENY
            ).exists()
        )

    def test_admin_can_create_group_and_grant(self):
        from apps.groups.models import Group
        from apps.permissions.constants import SubjectType
        from apps.permissions.models import ResourceACL

        self.client.force_login(self.alice)
        url = reverse("web:resource_permissions", args=[self.workspace.resource_id])
        self.client.post(url, {"action": "create_group", "group_name": "Platform"}, follow=True)
        group = Group.objects.get(name="Platform")
        self.assertTrue(
            ResourceACL.objects.filter(
                resource=self.workspace.resource,
                subject_type=SubjectType.GROUP,
                subject_id=group.id,
            ).exists()
        )

    def test_directory_search_lists_users(self):
        self.client.force_login(self.alice)
        response = self.client.get(
            reverse("web:resource_permissions", args=[self.workspace.resource_id]) + "?q=bob"
        )
        labels = [row["label"] for row in response.context["search"]["users"]]
        self.assertTrue(any("bob" in label for label in labels))
