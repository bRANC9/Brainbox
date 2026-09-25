"""DRF authentication using per-user API/MCP keys."""

from django.utils import timezone
from rest_framework.authentication import BaseAuthentication
from rest_framework.exceptions import AuthenticationFailed

from .models import ApiKey


class ApiKeyAuthentication(BaseAuthentication):
    """Authenticates `Authorization: ApiKey <raw>` or `X-API-Key: <raw>`.

    On success `request.user` is the key owner and `request.auth` is the
    :class:`ApiKey` instance, which the permission engine uses to narrow scope.
    """

    keyword = "ApiKey"
    header = "HTTP_X_API_KEY"

    def authenticate(self, request):
        raw_key = self._extract_key(request)
        if not raw_key:
            return None

        prefix = raw_key[:16]
        now = timezone.now()
        for key in ApiKey.objects.filter(key_prefix=prefix).select_related("user"):
            if key.matches(raw_key):
                if not key.is_usable:
                    raise AuthenticationFailed("API key is inactive, revoked or expired.")
                ApiKey.objects.filter(pk=key.pk).update(last_used_at=now)
                return (key.user, key)
        raise AuthenticationFailed("Invalid API key.")

    def _extract_key(self, request) -> str | None:
        header = request.META.get("HTTP_AUTHORIZATION", "")
        if header.startswith(f"{self.keyword} "):
            return header[len(self.keyword) + 1 :].strip() or None
        value = request.META.get(self.header, "")
        return value.strip() or None

    def authenticate_header(self, request) -> str:
        return self.keyword
