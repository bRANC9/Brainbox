"""Application services for accounts / API keys."""

from django.db import transaction

from apps.audit.models import AuditAction, AuditResult, AuditSource
from apps.audit.services import AuditService

from .models import ApiKey, ApiKeyScope


class ApiKeyService:
    @staticmethod
    @transaction.atomic
    def create(*, user, name, scopes=None, expires_at=None, actor=None, request=None):
        """Create a key and return ``(api_key, raw_key)``.

        ``scopes`` is a list of dicts accepted by :class:`ApiKeyScope`
        (``workspace``/``project``/``permission``/``effect``).
        """
        raw_key = ApiKey.generate_raw_key()
        api_key = ApiKey(user=user, name=name, expires_at=expires_at)
        api_key.set_key(raw_key)
        api_key.full_clean(exclude=["key_prefix", "key_hash"])
        api_key.save()

        for scope in scopes or []:
            ApiKeyScope.objects.create(api_key=api_key, **scope)

        AuditService.log(
            AuditAction.CREATE_API_KEY,
            user=actor or user,
            result=AuditResult.SUCCESS,
            source=AuditSource.WEB if request is None else AuditSource.API,
            request=request,
            detail={"api_key_id": str(api_key.id), "name": api_key.name},
        )
        return api_key, raw_key
