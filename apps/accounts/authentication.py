"""API/MCP key authentication.

Shared by DRF (`ApiKeyAuthentication`) and the MCP endpoint (plain Django).
"""

from datetime import timedelta

from django.db import transaction
from django.utils import timezone
from rest_framework.authentication import BaseAuthentication
from rest_framework.exceptions import AuthenticationFailed

from .models import ApiKey


def extract_api_key(request) -> str | None:
    header = request.META.get("HTTP_AUTHORIZATION", "")
    if header.startswith("ApiKey "):
        return header[len("ApiKey ") :].strip() or None
    value = request.META.get("HTTP_X_API_KEY", "")
    return value.strip() or None


def _consume_budget(candidate: ApiKey) -> None:
    """Charge one request against the key's budget, or refuse.

    A read-modify-write in a transaction. A lost increment under heavy
    concurrency would only make the budget marginally more permissive, which is
    the safe direction for a limit nobody is racing against.
    """
    now = timezone.now()
    with transaction.atomic():
        key = ApiKey.objects.get(pk=candidate.pk)
        if key.request_budget is not None:
            window = timedelta(seconds=key.budget_window_seconds or 86400)
            if key.budget_reset_at is None or now >= key.budget_reset_at:
                key.budget_used = 0
                key.budget_reset_at = now + window
            if key.budget_used >= key.request_budget:
                raise AuthenticationFailed("API key request budget exceeded.")
            key.budget_used += 1
        key.last_used_at = now
        key.save(update_fields=["last_used_at", "budget_used", "budget_reset_at"])
        candidate.last_used_at = key.last_used_at
        candidate.budget_used = key.budget_used
        candidate.budget_reset_at = key.budget_reset_at


def resolve_api_key(request):
    """Return ``(user, api_key)`` for the request, or ``(None, None)``.

    Raises ``AuthenticationFailed`` when a key is supplied but invalid/expired,
    or when it has spent its request budget.
    """
    raw_key = extract_api_key(request)
    if not raw_key:
        return (None, None)

    prefix = raw_key[:16]
    for key in ApiKey.objects.filter(key_prefix=prefix).select_related("user"):
        if key.matches(raw_key):
            if not key.is_usable:
                raise AuthenticationFailed("API key is inactive, revoked or expired.")
            _consume_budget(key)
            return (key.user, key)
    raise AuthenticationFailed("Invalid API key.")


class ApiKeyAuthentication(BaseAuthentication):
    """DRF authentication using per-user API/MCP keys."""

    keyword = "ApiKey"

    def authenticate(self, request):
        if not extract_api_key(request):
            return None
        return resolve_api_key(request)

    def authenticate_header(self, request) -> str:
        return self.keyword
