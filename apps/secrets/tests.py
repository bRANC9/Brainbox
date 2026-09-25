from django.core.exceptions import PermissionDenied
from django.test import TestCase

from apps.accounts.models import User
from apps.audit.models import AuditEvent
from apps.secrets.scanner import scan_text
from apps.secrets.services import SecretService
from apps.workspaces.services import WorkspaceService


class SecretVaultTests(TestCase):
    def setUp(self):
        self.alice = User.objects.create_user("alice", "alice@example.com", "pw")
        self.bob = User.objects.create_user("bob", "bob@example.com", "pw")

    def test_encrypted_roundtrip_and_reveal(self):
        secret = SecretService.create(
            owner=self.alice,
            name="azure-prod",
            payload={"client_id": "abc", "client_secret": "topsecret"},
            secret_type="client_credentials",
        )
        self.assertNotIn("topsecret", secret.encrypted_payload)

        value = SecretService.reveal(secret, user=self.alice)
        self.assertIn("topsecret", value)
        secret.refresh_from_db()
        self.assertIsNotNone(secret.last_used_at)

    def test_only_owner_can_use(self):
        secret = SecretService.create(owner=self.alice, name="s", payload="v")
        with self.assertRaises(PermissionDenied):
            SecretService.reveal(secret, user=self.bob)

    def test_attachment_scopes_usage(self):
        company = WorkspaceService.create(name="Company", created_by=self.alice)
        personal = WorkspaceService.create(name="Personal", created_by=self.alice)
        secret = SecretService.create(owner=self.alice, name="s", payload="v")
        SecretService.attach(secret=secret, workspace=company)

        self.assertTrue(SecretService.can_use(secret, self.alice, workspace_id=company.pk))
        self.assertFalse(SecretService.can_use(secret, self.alice, workspace_id=personal.pk))
        with self.assertRaises(PermissionDenied):
            SecretService.reveal(secret, user=self.alice, workspace_id=personal.pk)

    def test_use_is_audited_without_value(self):
        secret = SecretService.create(owner=self.alice, name="s", payload="hunter2")
        SecretService.reveal(secret, user=self.alice)
        event = AuditEvent.objects.filter(action="use_secret", user=self.alice).first()
        self.assertIsNotNone(event)
        self.assertNotIn("hunter2", str(event.detail))

    def test_scanner_detects_and_redacts(self):
        findings = scan_text("aws_access_key_id = AKIAABCDEFGHIJKLMNOP")
        self.assertTrue(findings)
        self.assertEqual(findings[0]["type"], "aws_access_key")
        self.assertIn("…", findings[0]["match"])
        self.assertNotIn("AKIAABCDEFGHIJKLMNOP", findings[0]["match"])
