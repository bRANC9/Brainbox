"""Event fan-out and delivery, plus saved-search watches.

Emit writes one delivery row per matching webhook, decoupled from sending, so a
slow or failed receiver never blocks the write that caused the event, and a
retry is just another pass over the pending rows. Sending goes through the
gateway, so it inherits the allowlist, the credential injection and the audit.
"""

from __future__ import annotations

import hashlib
import hmac
import json

from django.db import models
from django.utils import timezone

from apps.gateway.services import GatewayService
from apps.secrets.services import SecretService

from .models import SavedSearch, Webhook, WebhookDelivery, WebhookDeliveryStatus

MAX_ATTEMPTS = 5


class EventService:
    @classmethod
    def emit(cls, event, *, payload=None, workspace=None, resource=None, webhooks=None):
        """Queue a delivery for every enabled webhook that wants this event.

        ``workspace`` scopes it: a webhook with no workspace is global, one with
        a workspace only hears about that workspace. ``webhooks`` narrows to an
        explicit set (a saved search notifying its own subscription).
        """
        queryset = Webhook.objects.filter(enabled=True)
        if workspace is not None:
            queryset = queryset.filter(
                models.Q(workspace__isnull=True) | models.Q(workspace=workspace)
            )
        if webhooks is not None:
            queryset = queryset.filter(pk__in=[webhook.pk for webhook in webhooks])

        deliveries = []
        for webhook in queryset:
            if not webhook.wants(event):
                continue
            deliveries.append(
                WebhookDelivery.objects.create(
                    webhook=webhook, event=event, payload=payload or {}
                )
            )
        return deliveries

    @classmethod
    def deliver_pending(cls, *, limit: int = 50) -> dict:
        pending = (
            WebhookDelivery.objects.filter(
                status=WebhookDeliveryStatus.PENDING, attempts__lt=MAX_ATTEMPTS
            )
            .select_related("webhook", "webhook__target", "webhook__owner")
            .order_by("created_at")[:limit]
        )
        sent = failed = 0
        for delivery in pending:
            if cls._deliver(delivery):
                sent += 1
            else:
                failed += 1
        return {"sent": sent, "failed": failed}

    @classmethod
    def _deliver(cls, delivery: WebhookDelivery) -> bool:
        webhook = delivery.webhook
        # Sign exactly the bytes that get sent: a string body is passed through
        # verbatim, so the receiver can recompute the same HMAC.
        raw = json.dumps(
            {
                "event": delivery.event,
                "payload": delivery.payload,
                "delivered_at": timezone.now().isoformat(),
            },
            default=str,
        )
        headers = {"Content-Type": "application/json", "X-Brainbox-Event": delivery.event}
        if webhook.signing_secret_id:
            secret = SecretService.reveal(webhook.signing_secret, user=webhook.owner)
            signature = hmac.new(
                secret.encode("utf-8"), raw.encode("utf-8"), hashlib.sha256
            ).hexdigest()
            headers["X-Brainbox-Signature"] = f"sha256={signature}"

        try:
            result = GatewayService.call(
                webhook.target,
                method="POST",
                path="",
                body=raw,
                headers=headers,
                user=webhook.owner,
            )
        except Exception as exc:  # noqa: BLE001 - any failure is retryable
            return cls._record_failure(delivery, str(exc))

        if 200 <= result["status"] < 300:
            delivery.status = WebhookDeliveryStatus.SENT
            delivery.sent_at = timezone.now()
            delivery.attempts += 1
            delivery.save(update_fields=["status", "sent_at", "attempts"])
            return True
        return cls._record_failure(delivery, f"HTTP {result['status']}")

    @staticmethod
    def _record_failure(delivery: WebhookDelivery, error: str) -> bool:
        delivery.attempts += 1
        delivery.last_error = error[:500]
        delivery.status = (
            WebhookDeliveryStatus.FAILED
            if delivery.attempts >= MAX_ATTEMPTS
            else WebhookDeliveryStatus.PENDING
        )
        delivery.save(update_fields=["attempts", "last_error", "status"])
        return False


class SavedSearchService:
    @classmethod
    def check(cls, saved_search: SavedSearch) -> list:
        """Run the query as its owner and notify on anything not seen before."""
        from apps.search.services import SearchService

        results = SearchService.search(
            saved_search.owner,
            saved_search.query,
            mode=saved_search.mode,
            workspace_id=saved_search.workspace_id,
            limit=20,
        )
        seen = {str(identifier) for identifier in (saved_search.last_seen_ids or [])}
        fresh = [row for row in results if str(row.get("id")) not in seen]
        if fresh:
            EventService.emit(
                "saved_search.match",
                payload={
                    "saved_search": saved_search.name,
                    "matches": [
                        {
                            "id": str(row.get("id")),
                            "title": row.get("title"),
                            "path": row.get("path"),
                        }
                        for row in fresh
                    ],
                },
                workspace=saved_search.workspace,
                webhooks=[saved_search.webhook] if saved_search.webhook_id else None,
            )
        saved_search.last_seen_ids = list(
            seen | {str(row.get("id")) for row in results}
        )
        saved_search.last_checked_at = timezone.now()
        saved_search.save(
            update_fields=["last_seen_ids", "last_checked_at", "updated_at"]
        )
        return fresh

    @classmethod
    def check_all(cls) -> dict:
        checked = notified = 0
        for saved_search in SavedSearch.objects.filter(enabled=True).select_related(
            "owner", "webhook", "workspace"
        ):
            if cls.check(saved_search):
                notified += 1
            checked += 1
        return {"checked": checked, "notified": notified}
