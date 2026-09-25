from django.test import TestCase, override_settings
from django.urls import reverse

from apps.accounts.models import User
from apps.accounts.oidc import get_or_create_user


@override_settings(
    OIDC_ENABLED=True,
    OIDC_ISSUER="https://idp.example",
    OIDC_CLIENT_ID="client",
    OIDC_CLIENT_SECRET="secret",
    OIDC_AUTO_CREATE_USERS=True,
)
class OIDCTests(TestCase):
    def test_login_redirects_to_provider(self):
        response = self.client.get(reverse("accounts:oidc_login"))
        self.assertEqual(response.status_code, 302)
        self.assertIn("idp.example", response.url)
        self.assertIn("state=", response.url)

    def test_links_existing_user_by_email(self):
        existing = User.objects.create_user("alice", "alice@example.com", "pw")
        user = get_or_create_user({"sub": "abc", "email": "ALICE@example.com", "name": "Alice"})
        self.assertEqual(user.pk, existing.pk)
        existing.refresh_from_db()
        self.assertEqual(existing.oidc_subject, "abc")
        self.assertEqual(existing.oidc_issuer, "https://idp.example")

    def test_creates_new_user(self):
        user = get_or_create_user({"sub": "xyz", "email": "bob@example.com", "name": "Bob"})
        self.assertEqual(user.username, "bob")
        self.assertEqual(user.oidc_subject, "xyz")

    def test_disabled_returns_404(self):
        with override_settings(OIDC_ENABLED=False):
            response = self.client.get(reverse("accounts:oidc_login"))
        self.assertEqual(response.status_code, 404)
