import tempfile
from unittest.mock import patch

from django.test import TestCase, override_settings

from apps.accounts.models import User
from apps.gateway.services import GatewayService
from apps.secrets.services import SecretService
from apps.workspaces.services import WorkspaceService

from .models import SavedSearch, Webhook, WebhookDelivery, WebhookDeliveryStatus
from .services import EventService, SavedSearchService


@override_settings(KNOWLEDGE_DATA_ROOT=tempfile.mkdtemp())
class EventDeliveryTests(TestCase):
    def setUp(self):
        self.alice = User.objects.create_user("alice", "alice@example.com", "pw")
        self.workspace = WorkspaceService.create(name="Company", created_by=self.alice)
        self.target = GatewayService.create(
            name="hook",
            base_url="https://hooks.example.com",
            workspace=self.workspace,
            created_by=self.alice,
        )
        self.secret = SecretService.create(
            owner=self.alice, name="sig", payload="topsecret"
        )
        self.webhook = Webhook.objects.create(
            name="CI",
            owner=self.alice,
            workspace=self.workspace,
            target=self.target,
            signing_secret=self.secret,
            events=["document.status_changed"],
        )

    def test_emit_matches_event_and_workspace(self):
        deliveries = EventService.emit(
            "document.status_changed", payload={"x": 1}, workspace=self.workspace
        )
        self.assertEqual(len(deliveries), 1)
        self.assertEqual(EventService.emit("other.event", workspace=self.workspace), [])

    def test_delivery_signs_and_marks_sent(self):
        EventService.emit(
            "document.status_changed", payload={"x": 1}, workspace=self.workspace
        )
        captured = {}

        def fake_send(method, url, headers, body, max_bytes):
            captured.update(method=method, headers=headers, body=body)
            return 200, {}, "ok", False

        with patch.object(GatewayService, "_send", fake_send):
            result = EventService.deliver_pending()

        self.assertEqual(result["sent"], 1)
        delivery = WebhookDelivery.objects.get()
        self.assertEqual(delivery.status, WebhookDeliveryStatus.SENT)
        signature = captured["headers"]["X-Brainbox-Signature"]
        self.assertTrue(signature.startswith("sha256="))
        # The signed body is what was sent, verbatim.
        self.assertIn("document.status_changed", captured["body"])

    def test_a_failing_receiver_is_retried_then_failed(self):
        EventService.emit("document.status_changed", payload={}, workspace=self.workspace)
        with patch.object(GatewayService, "_send", lambda *a, **k: (500, {}, "err", False)):
            for _ in range(5):
                EventService.deliver_pending()
        delivery = WebhookDelivery.objects.get()
        self.assertEqual(delivery.status, WebhookDeliveryStatus.FAILED)
        self.assertEqual(delivery.attempts, 5)

    def test_a_disabled_webhook_gets_nothing(self):
        self.webhook.enabled = False
        self.webhook.save(update_fields=["enabled"])
        self.assertEqual(
            EventService.emit("document.status_changed", workspace=self.workspace), []
        )


@override_settings(KNOWLEDGE_DATA_ROOT=tempfile.mkdtemp())
class SavedSearchTests(TestCase):
    def setUp(self):
        self.alice = User.objects.create_user("alice", "alice@example.com", "pw")
        self.workspace = WorkspaceService.create(name="Company", created_by=self.alice)
        self.target = GatewayService.create(
            name="hook",
            base_url="https://hooks.example.com",
            workspace=self.workspace,
            created_by=self.alice,
        )
        self.webhook = Webhook.objects.create(
            name="CI",
            owner=self.alice,
            workspace=self.workspace,
            target=self.target,
            events=["saved_search.match"],
        )
        self.saved = SavedSearch.objects.create(
            name="runbooks",
            owner=self.alice,
            workspace=self.workspace,
            query="deploy",
            webhook=self.webhook,
        )

    def test_only_new_matches_are_reported(self):
        rows = [{"id": "1", "title": "A", "path": "a.md"}]
        with patch("apps.search.services.SearchService.search", return_value=rows):
            fresh = SavedSearchService.check(self.saved)
        self.assertEqual(len(fresh), 1)
        self.assertEqual(
            WebhookDelivery.objects.filter(event="saved_search.match").count(), 1
        )
        # A second run finds nothing new.
        with patch("apps.search.services.SearchService.search", return_value=rows):
            self.assertEqual(SavedSearchService.check(self.saved), [])
