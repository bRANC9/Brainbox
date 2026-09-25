"""Audit logging helper."""

from __future__ import annotations

from .models import AuditAction, AuditEvent, AuditResult, AuditSource


class AuditService:
    @staticmethod
    def log(
        action: str,
        *,
        user=None,
        api_key=None,
        resource=None,
        workspace=None,
        project=None,
        source: str = AuditSource.SYSTEM,
        result: str = AuditResult.SUCCESS,
        request=None,
        detail: dict | None = None,
        version=None,
        git_commit: str = "",
    ) -> AuditEvent:
        ip_address = None
        user_agent = ""

        if request is not None:
            forwarded = request.META.get("HTTP_X_FORWARDED_FOR", "")
            ip_address = (
                forwarded.split(",")[0].strip() if forwarded else request.META.get("REMOTE_ADDR")
            )
            user_agent = request.META.get("HTTP_USER_AGENT", "")[:512]
            if api_key is None:
                auth = getattr(request, "auth", None)
                if auth is not None and auth.__class__.__name__ == "ApiKey":
                    api_key = auth

        return AuditEvent.objects.create(
            action=action,
            user=user if getattr(user, "is_authenticated", False) else None,
            api_key=api_key,
            resource=resource,
            workspace=workspace,
            project=project,
            source=source,
            result=result,
            ip_address=ip_address,
            user_agent=user_agent,
            detail=detail or {},
            version=version,
            git_commit=git_commit,
        )


__all__ = [
    "AuditService",
    "AuditAction",
    "AuditResult",
    "AuditSource",
]
