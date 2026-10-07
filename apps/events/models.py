"""Outbound events: webhooks and saved-search watches.

The system already records everything in the audit trail, but nothing tells an
outside system that something happened. A webhook subscribes to named events and
receives a signed POST; delivery goes through a gateway target, so the URL, the
allowlist and any credential live in one place and every delivery is audited
like any other egress. A saved search is the same machinery pointed at a query:
it re-runs on a schedule and notifies when something new matches.
"""

from __future__ import annotations

import uuid

from django.conf import settings
from django.db import models


class Webhook(models.Model):
    """A subscription: which events, delivered where, signed with what."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    name = models.CharField(max_length=255)
    owner = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="webhooks",
    )
    workspace = models.ForeignKey(
        "workspaces.Workspace",
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="webhooks",
    )
    target = models.ForeignKey(
        "gateway.GatewayTarget",
        on_delete=models.CASCADE,
        related_name="webhooks",
        help_text="The egress target the POST is sent through.",
    )
    signing_secret = models.ForeignKey(
        "secrets.Secret",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="webhooks",
        help_text="Optional HMAC secret; the receiver verifies the signature.",
    )
    events = models.JSONField(
        default=list, blank=True, help_text='Event names, or "*" for all.'
    )
    enabled = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "events_webhook"
        ordering = ["name"]

    def __str__(self) -> str:
        return self.name

    def wants(self, event: str) -> bool:
        return "*" in (self.events or []) or event in (self.events or [])


class WebhookDeliveryStatus(models.TextChoices):
    PENDING = "pending", "Pending"
    SENT = "sent", "Sent"
    FAILED = "failed", "Failed"


class WebhookDelivery(models.Model):
    """One attempt-tracked delivery of one event to one webhook."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    webhook = models.ForeignKey(
        Webhook, on_delete=models.CASCADE, related_name="deliveries"
    )
    event = models.CharField(max_length=64)
    payload = models.JSONField(default=dict, blank=True)
    status = models.CharField(
        max_length=16,
        choices=WebhookDeliveryStatus.choices,
        default=WebhookDeliveryStatus.PENDING,
        db_index=True,
    )
    attempts = models.PositiveIntegerField(default=0)
    last_error = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    sent_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = "events_webhook_delivery"
        ordering = ["created_at"]

    def __str__(self) -> str:
        return f"{self.event} -> {self.webhook_id} ({self.status})"


class SavedSearch(models.Model):
    """A stored query the system re-runs, notifying on new matches."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    name = models.CharField(max_length=255)
    owner = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="saved_searches"
    )
    workspace = models.ForeignKey(
        "workspaces.Workspace",
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="saved_searches",
    )
    query = models.CharField(max_length=512)
    mode = models.CharField(max_length=16, default="hybrid")
    enabled = models.BooleanField(default=True)
    webhook = models.ForeignKey(
        Webhook,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="saved_searches",
    )
    last_checked_at = models.DateTimeField(null=True, blank=True)
    last_seen_ids = models.JSONField(default=list, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "events_saved_search"
        ordering = ["name"]

    def __str__(self) -> str:
        return self.name
