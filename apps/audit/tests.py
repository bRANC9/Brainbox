from django.test import TestCase

from apps.accounts.models import User
from apps.audit.models import AuditAction, AuditEvent, AuditSource
from apps.audit.services import AuditService


class AuditEventTests(TestCase):
    def setUp(self):
        self.alice = User.objects.create_user("alice", "alice@example.com", "pw")

    def test_log_creates_event(self):
        event = AuditService.log(
            AuditAction.CREATE, user=self.alice, detail={"type": "workspace"}
        )
        self.assertEqual(event.user_id, self.alice.pk)
        self.assertEqual(event.source, AuditSource.SYSTEM)
        self.assertEqual(event.detail["type"], "workspace")

    def test_anonymous_user_is_not_stored(self):
        event = AuditService.log(AuditAction.SEARCH, user=None)
        self.assertIsNone(event.user)

    def test_append_only_cannot_be_modified(self):
        event = AuditService.log(AuditAction.UPDATE, user=self.alice)
        event.detail = {"tampered": True}
        with self.assertRaises(ValueError):
            event.save()

    def test_reads_metadata_from_request(self):
        from django.test import RequestFactory

        request = RequestFactory().post("/x/", HTTP_X_FORWARDED_FOR="1.2.3.4, 5.6.7.8")
        request.META["HTTP_USER_AGENT"] = "test-agent"
        event = AuditService.log(AuditAction.READ, user=self.alice, request=request)
        self.assertEqual(event.ip_address, "1.2.3.4")  # first entry of the chain
        self.assertEqual(event.user_agent, "test-agent")

    def test_denied_result_recorded(self):
        from apps.audit.models import AuditResult

        event = AuditService.log(
            AuditAction.USE_SECRET, user=self.alice, result=AuditResult.DENIED
        )
        self.assertEqual(event.result, AuditResult.DENIED)

    def test_ordering_is_newest_first(self):
        first = AuditService.log(AuditAction.READ, user=self.alice)
        second = AuditService.log(AuditAction.UPDATE, user=self.alice)
        self.assertEqual(list(AuditEvent.objects.all()), [second, first])