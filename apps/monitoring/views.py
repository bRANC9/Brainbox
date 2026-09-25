"""Readiness probe used by orchestrators (complements the /healthz liveness)."""

from __future__ import annotations

from pathlib import Path

from django.conf import settings
from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.http import JsonResponse
from django.views.decorators.http import require_GET


@require_GET
def readyz(request):
    checks: dict[str, str] = {}
    healthy = True

    try:
        with connection.cursor() as cursor:
            cursor.execute("SELECT 1")
            cursor.fetchone()
        checks["database"] = "ok"
    except Exception:  # noqa: BLE001
        checks["database"] = "error"
        healthy = False

    try:
        executor = MigrationExecutor(connection)
        pending = executor.migration_plan(executor.loader.graph.leaf_nodes())
        checks["migrations"] = "pending" if pending else "ok"
        if pending:
            checks["migrations_count"] = str(len(pending))
            healthy = False
    except Exception:  # noqa: BLE001
        checks["migrations"] = "error"
        healthy = False

    try:
        root = Path(settings.KNOWLEDGE_DATA_ROOT)
        root.mkdir(parents=True, exist_ok=True)
        probe = root / ".readiness"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
        checks["storage"] = "ok"
    except Exception:  # noqa: BLE001
        checks["storage"] = "error"
        healthy = False

    return JsonResponse(
        {"status": "ok" if healthy else "degraded", **checks},
        status=200 if healthy else 503,
    )
