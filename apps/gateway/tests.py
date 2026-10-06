import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import patch

from django.core.exceptions import PermissionDenied, ValidationError
from django.test import SimpleTestCase, TestCase, override_settings

from apps.accounts.models import User
from apps.permissions.constants import Permission, SubjectType
from apps.permissions.services import PermissionService
from apps.secrets.services import SecretService
from apps.workspaces.services import WorkspaceService

from .models import GatewayKind
from .services import GatewayService, RateLimited, parse_sse_json


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

        def fake_send(method, url, headers, body, max_bytes):
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

    def test_unknown_config_key_is_rejected(self):
        # A misspelt key used to fall through to the default silently.
        with self.assertRaises(ValidationError):
            GatewayService.create(
                name="Typo",
                base_url="https://api.github.com",
                config={"allow_host": ["api.github.com"]},
                created_by=self.alice,
            )

    def test_query_auth_is_rejected(self):
        with self.assertRaises(ValidationError):
            GatewayService.create(
                name="Query",
                base_url="https://api.github.com",
                config={"auth": "query"},
                created_by=self.alice,
            )

    def test_rate_limit_must_be_positive_int(self):
        with self.assertRaises(ValidationError):
            GatewayService.create(
                name="Bad",
                base_url="https://api.github.com",
                config={"rate_limit": -1},
                created_by=self.alice,
            )

    def test_rate_limit_blocks_after_the_cap(self):
        target = GatewayService.create(
            name="Limited",
            base_url="https://api.github.com",
            config={"rate_limit": 1, "rate_window_seconds": 60},
            workspace=self.workspace,
            created_by=self.alice,
        )
        with patch.object(GatewayService, "_send", lambda *a, **k: (200, {}, "{}", False)):
            GatewayService.call(target, user=self.alice)
            with self.assertRaises(RateLimited):
                GatewayService.call(target, user=self.alice)

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
            GatewayService, "_send", lambda *a, **k: (200, {}, "{}", False)
        ):
            result = GatewayService.call(self.target, user=self.bob)
        self.assertEqual(result["status"], 200)

    def test_stream_requires_allow_stream(self):
        target = GatewayService.create(
            name="NoStream",
            base_url="http://127.0.0.1:1",
            config={"allow_private": True},
            workspace=self.workspace,
            created_by=self.alice,
        )
        with self.assertRaises(ValidationError):
            GatewayService.stream(target, user=self.alice)

    def test_stream_refuses_a_target_with_a_secret(self):
        target = GatewayService.create(
            name="SecretStream",
            base_url="http://127.0.0.1:1",
            config={"allow_private": True, "allow_stream": True},
            secret=self.secret,
            workspace=self.workspace,
            created_by=self.alice,
        )
        with self.assertRaises(ValidationError):
            GatewayService.stream(target, user=self.alice)


class ParseSseTests(SimpleTestCase):
    def test_extracts_json_messages_and_skips_the_rest(self):
        body = (
            "event: message\n"
            'data: {"jsonrpc": "2.0", "id": 1, "result": {"ok": true}}\n\n'
            "data: [DONE]\n\n"
            ": heartbeat\n\n"
            "data: not json\n\n"
        )
        self.assertEqual(
            parse_sse_json(body),
            [{"jsonrpc": "2.0", "id": 1, "result": {"ok": True}}],
        )
        self.assertEqual(parse_sse_json("not sse at all"), [])


class _StreamingHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
        self.end_headers()
        self.wfile.write(b"hello ")
        self.wfile.write(b"world")

    def log_message(self, *args):
        pass


@override_settings(KNOWLEDGE_DATA_ROOT=tempfile.mkdtemp())
class GatewayStreamTests(TestCase):
    """A real local server: the stream path is the one place a mock would lie."""

    def setUp(self):
        self.alice = User.objects.create_user("alice", "alice@example.com", "pw")
        self.workspace = WorkspaceService.create(name="Company", created_by=self.alice)
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), _StreamingHandler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        port = self.server.server_address[1]
        self.target = GatewayService.create(
            name="Local",
            base_url=f"http://127.0.0.1:{port}",
            config={"allow_private": True, "allow_stream": True},
            workspace=self.workspace,
            created_by=self.alice,
        )

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()

    def test_stream_forwards_the_body(self):
        status, _headers, chunks = GatewayService.stream(self.target, user=self.alice)
        self.assertEqual(status, 200)
        self.assertEqual(b"".join(chunks), b"hello world")
