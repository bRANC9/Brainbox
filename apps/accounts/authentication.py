"""API/MCP key authentication.

Shared by DRF (`ApiKeyAuthentication`) and the MCP endpoint (plain Django).
"""

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


def resolve_api_key(request):
    """Return ``(user, api_key)`` for the request, or ``(None, None)``.

    Raises ``AuthenticationFailed`` when a key is supplied but invalid/expired.
    """
    raw_key = extract_api_key(request)
    if not raw_key:
        return (None, None)

    prefix = raw_key[:16]
    for key in ApiKey.objects.filter(key_prefix=prefix).select_related("user"):
        if key.matches(raw_key):
            if not key.is_usable:
                raise AuthenticationFailed("API key is inactive, revoked or expired.")
            ApiKey.objects.filter(pk=key.pk).update(last_used_at=timezone.now())
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
