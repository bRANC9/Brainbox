"""Access-control regressions found while auditing the user API and the admin.

Both were confirmed by reproduction first:

* ``SelfUserSerializer`` keeps ``password`` writable so people can change their
  own, but ``UserViewSet.get_queryset`` returned *every* user, so any
  authenticated user could PATCH anyone -- including a superuser -- and set a
  new password. Verified: alice rewrote bob's password and email with a 200.

* ``WorkspaceAdmin``/``ProjectAdmin`` 500'd on their add forms, because
  ``permission_link`` reverse()d with ``resource_id`` before it exists. And the
  POST could not work anyway: ``resource`` is the primary key and is readonly,
  so the insert died on a not-null violation.
"""

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from rest_framework import status
from rest_framework.test import APIClient, APITestCase

User = get_user_model()


class UserWriteScopeTests(APITestCase):
    """A non-superuser may write its own row and nobody else's."""

    def setUp(self):
        self.alice = User.objects.create_user("alice", "alice@example.com", "alice-pw")
        self.bob = User.objects.create_user("bob", "bob@example.com", "bob-pw")
        self.root = User.objects.create_superuser("root", "root@example.com", "root-pw")
        self.client = APIClient()
        self.client.force_authenticate(self.alice)

    def test_cannot_change_another_users_password(self):
        response = self.client.patch(
            f"/api/v1/users/{self.bob.pk}/", {"password": "pwned"}, format="json"
        )
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)
        self.bob.refresh_from_db()
        self.assertTrue(self.bob.check_password("bob-pw"), "bob's password changed")

    def test_cannot_change_a_superusers_password(self):
        response = self.client.patch(
            f"/api/v1/users/{self.root.pk}/", {"password": "pwned"}, format="json"
        )
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)
        self.root.refresh_from_db()
        self.assertTrue(self.root.check_password("root-pw"), "the superuser password changed")

    def test_cannot_change_another_users_email(self):
        """Email is the OIDC identity anchor -- apps.accounts.oidc links by it."""
        response = self.client.patch(
            f"/api/v1/users/{self.bob.pk}/", {"email": "attacker@evil.test"}, format="json"
        )
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)
        self.bob.refresh_from_db()
        self.assertEqual(self.bob.email, "bob@example.com")

    def test_can_change_own_password(self):
        """The self-service path the writable field exists for must keep working."""
        response = self.client.patch(
            f"/api/v1/users/{self.alice.pk}/", {"password": "new-pw"}, format="json"
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.alice.refresh_from_db()
        self.assertTrue(self.alice.check_password("new-pw"))

    def test_cannot_change_own_email(self):
        """Otherwise a user could claim another account's IdP identity."""
        response = self.client.patch(
            f"/api/v1/users/{self.alice.pk}/", {"email": "someone-else@example.com"},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.alice.refresh_from_db()
        self.assertEqual(self.alice.email, "alice@example.com")

    def test_cannot_escalate_own_privileges(self):
        response = self.client.patch(
            f"/api/v1/users/{self.alice.pk}/",
            {"is_superuser": True, "is_staff": True, "is_active": False},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.alice.refresh_from_db()
        self.assertFalse(self.alice.is_superuser)
        self.assertFalse(self.alice.is_staff)
        self.assertTrue(self.alice.is_active)

    def test_directory_search_still_finds_other_users(self):
        """Read access must survive: the access panel needs to find users."""
        response = self.client.get("/api/v1/users/?q=bob")
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertEqual([u["username"] for u in response.data["results"]], ["bob"])

    def test_superuser_can_still_manage_any_user(self):
        self.client.force_authenticate(self.root)
        response = self.client.patch(
            f"/api/v1/users/{self.bob.pk}/", {"password": "reset-by-admin"}, format="json"
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.bob.refresh_from_db()
        self.assertTrue(self.bob.check_password("reset-by-admin"))

    def test_cannot_delete_another_user(self):
        response = self.client.delete(f"/api/v1/users/{self.bob.pk}/")
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)


class ResourceBackedAdminTests(TestCase):
    """Resource-backed models must be creatable only through the service."""

    def setUp(self):
        from apps.workspaces.services import WorkspaceService

        self.root = User.objects.create_superuser("root", "root@example.com", "pw")
        self.workspace = WorkspaceService.create(name="Acme", created_by=self.root)
        self.client.force_login(self.root)

    def test_admin_add_view_is_refused_not_broken(self):
        """Used to 500 with NoReverseMatch; now admin refuses it properly.

        The POST could never have worked either -- ``resource`` is the readonly
        primary key, so the insert failed on a not-null violation. Creation goes
        through the service, which also issues the owner grant.
        """
        for name in ("workspace", "project"):
            with self.subTest(model=name):
                response = self.client.get(reverse(f"admin:workspaces_{name}_add"))
                self.assertEqual(response.status_code, 403)
                self.assertNotIn("NoReverseMatch", response.content.decode()[:2000])

    def test_admin_offers_no_add_button(self):
        """The POST could never work: resource is the readonly primary key."""
        for name in ("workspace", "project"):
            with self.subTest(model=name):
                response = self.client.get(reverse(f"admin:workspaces_{name}_changelist"))
                self.assertEqual(response.status_code, 200)
                self.assertNotContains(response, f'/admin/workspaces/{name}/add/')

    def test_permission_link_renders_for_a_saved_object(self):
        response = self.client.get(
            reverse("admin:workspaces_workspace_change", args=[self.workspace.pk])
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, f"/resources/{self.workspace.pk}/permissions/")