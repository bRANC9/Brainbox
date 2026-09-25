"""Liveness/readiness endpoint used by Docker/Traefik/Pangolin."""

from django.db import connection
from django.http import JsonResponse


def healthz(request):
    payload = {"status": "ok", "database": "ok"}
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
