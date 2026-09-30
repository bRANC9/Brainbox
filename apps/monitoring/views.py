"""Readiness probe used by orchestrators (complements the /healthz liveness)."""

from __future__ import annotations

from pathlib import Path

from django.conf import settings
from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.http import JsonResponse
from django.views.decorators.http import require_GET

#: Asset every page links, so its absence in STATIC_ROOT means "unstyled UI".
MARKER_ASSET = "css/app.css"


@require_GET
def readyz(request):
    """Readiness: DB + migrations + writable storage + AI provider reachability.

    ``?deep=1`` runs a real embedding/LLM call instead of a reachability check.
    A failing optional provider degrades the response but only fails the check
    when BRAINBOX_REQUIRE_EMBEDDING is on.
    """
    deep = request.GET.get("deep") in {"1", "true"}
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

    # Static assets are collected at boot by `collectstatic_safe`. A missing
    # marker file means the UI is being served unstyled, which is invisible from
    # outside unless it is reported here.
    try:
        static_root = Path(settings.STATIC_ROOT)
        if (static_root / MARKER_ASSET).exists():
            checks["static"] = "ok"
        else:
            checks["static"] = "missing"
            checks["static_dir"] = str(static_root)
            healthy = False
    except Exception:  # noqa: BLE001
        checks["static"] = "error"
        healthy = False

    # AI providers (optional dependency: degrade, don't kill the app).
    try:
        from apps.settings_store.probes import probe_embedding, probe_llm
        from apps.settings_store.services import get_value

        embedding = probe_embedding(deep=deep)
        checks["embedding"] = "ok" if embedding.get("ok") else f"error: {embedding.get('detail')}"
        if not embedding.get("ok"):
            if get_value("BRAINBOX_REQUIRE_EMBEDDING", False):
                healthy = False
        if deep:
            llm = probe_llm(deep=deep)
            checks["llm"] = "ok" if llm.get("ok") else f"error: {llm.get('detail')}"
    except Exception as exc:  # noqa: BLE001
        checks["embedding"] = f"error: {exc}"

    return JsonResponse(
        {"status": "ok" if healthy else "degraded", **checks},
        status=200 if healthy else 503,
    )
