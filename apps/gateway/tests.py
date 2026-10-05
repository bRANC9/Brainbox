import tempfile
from unittest.mock import patch

from django.core.exceptions import PermissionDenied, ValidationError
from django.test import TestCase, override_settings

from apps.accounts.models import User
from apps.permissions.constants import Permission, SubjectType
from apps.permissions.services import PermissionService
from apps.secrets.services import SecretService
from apps.workspaces.services import WorkspaceService

from .models import GatewayKind
from .services import GatewayService


@override_settings(KNOWLEDGE_DATA_ROOT=tempfile.mkdtemp())
class GatewayServiceTests(TestCase):
    def setUp(self):
        self.alice = User.objects.create_user("alice", "alice@example.com", "pw")
        self.bob = User.objects.create_user("bob", "bob@example.com", "pw")
        self.workspace = WorkspaceService.create(name="Company", created_by=self.alice)
        self.secret = SecretService.create(
            owner=self.alice,
            name="api-token",
            payload="s3cr3t-value",
            secret_type="bearer_token",
        )
        self.target = GatewayService.create(
            name="GitHub",
            base_url="https://api.github.com",
            kind=GatewayKind.HTTP,
            config={"auth": "bearer"},
            secret=self.secret,
            workspace=self.workspace,
            created_by=self.alice,
        )

    def test_use_permission_is_required(self):
        with self.assertRaises(PermissionDenied):
            GatewayService.call(self.target, user=self.bob)

    def test_secret_is_injected_and_redacted(self):
        captured = {}

        def fake_send(method, url, headers, body):
            captured.update(method=method, url=url, headers=headers)
            return 200, {"Content-Type": "text/plain"}, "token=s3cr3t-value ok", False

        with patch.object(GatewayService, "_send", fake_send):
            result = GatewayService.call(
                self.target, method="GET", path="user", user=self.alice
            )
        self.assertEqual(captured["url"], "https://api.github.com/user")
        self.assertEqual(captured["headers"]["Authorization"], "Bearer s3cr3t-value")
        # The credential must not come back on the wire.
        self.assertNotIn("s3cr3t-value", result["body"])
        self.assertIn("***", result["body"])

    def test_method_allowlist(self):
        with self.assertRaises(ValidationError):
            GatewayService.call(self.target, method="DELETE", user=self.alice)

    def test_host_allowlist(self):
        target = GatewayService.create(
            name="Bad",
            base_url="https://api.github.com",
            config={"allow_hosts": ["other.example.com"]},
            workspace=self.workspace,
            created_by=self.alice,
        )
        with self.assertRaises(ValidationError):
            GatewayService.call(target, user=self.alice)

    def test_private_host_is_blocked(self):
        target = GatewayService.create(
            name="Local",
            base_url="http://127.0.0.1:9999",
            workspace=self.workspace,
            created_by=self.alice,
        )
        with self.assertRaises(ValidationError):
            GatewayService.call(target, user=self.alice)

    def test_grant_use_to_another_user(self):
        PermissionService.grant(
            self.target.resource,
            subject_type=SubjectType.USER,
            subject_id=self.bob.id,
            permission=Permission.USE,
            created_by=self.alice,
        )
        with patch.object(
            GatewayService, "_send", lambda method, url, headers, body: (200, {}, "{}", False)
        ):
            result = GatewayService.call(self.target, user=self.bob)
        self.assertEqual(result["status"], 200)
