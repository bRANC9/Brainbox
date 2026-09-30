"""Liveness/readiness endpoint used by Docker/Traefik/Pangolin."""

from django.conf import settings
from django.db import connection
from django.http import JsonResponse


def _build_identity() -> str:
    """The commit the running image was built from (``"dev"`` if not baked in)."""
    return str(getattr(settings, "APP_GIT_SHA", "") or "")


def healthz(request):
    payload = {"status": "ok", "database": "ok", "git_sha": _build_identity()}
    try:
        with connection.cursor() as cursor:
            cursor.execute("SELECT 1")
            cursor.fetchone()
    except Exception as exc:  # pragma: no cover - depends on environment
        payload["status"] = "degraded"
        payload["database"] = "error"
        payload["detail"] = str(exc)
        return JsonResponse(payload, status=503)
    return JsonResponse(payload)
