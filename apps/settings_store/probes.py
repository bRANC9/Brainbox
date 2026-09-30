"""Connectivity probes for the configured AI providers.

Used by the settings page ("tesztelés" button), the ``provider_check``
management command and the readiness endpoint. Shallow probes only check that
the endpoint answers (fast, safe for health checks); deep probes run a real
embedding/LLM call.
"""

from __future__ import annotations

import urllib.error
import urllib.request

DEFAULT_TIMEOUT = 5.0
DEEP_TIMEOUT = 90.0


def _reachable(base_url: str, timeout: float = DEFAULT_TIMEOUT) -> tuple[bool, str]:
    url = base_url.rstrip("/") + "/models"
    request = urllib.request.Request(url, headers={"Accept": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=timeout):
            return True, "endpoint elérhető"
    except urllib.error.HTTPError as exc:
        return True, f"endpoint elérhető (HTTP {exc.code})"
    except (urllib.error.URLError, TimeoutError, ValueError) as exc:
        return False, f"nem elérhető: {exc}"


def probe_embedding(deep: bool = False) -> dict:
    """Shallow: is the endpoint up. Deep: run a real embedding call."""
    from apps.embeddings.providers import EmbeddingError, get_embedding_provider
    from apps.settings_store.services import get_value

    provider = get_embedding_provider()
    result = {
        "name": provider.name,
        "model": getattr(provider, "model", ""),
        "base_url": get_value("OPENAI_BASE_URL", ""),
    }
    if provider.name == "deterministic":
        result.update({"ok": True, "detail": "offline (deterministic provider)"})
        return result

    if not deep:
        ok, detail = _reachable(str(result["base_url"]))
        result.update({"ok": ok, "detail": detail})
        return result

    try:
        vectors = provider.embed(["brainbox probe"])
        measured = len(vectors[0])
        configured = int(get_value("BRAINBOX_EMBEDDING_DIM", 256) or 256)
        detail = f"OK, mért dimenzió: {measured}"
        if measured != configured:
            detail += f" (beállított: {configured} – a local store-on nem gond, Qdrantnál fontos)"
        result.update({"ok": True, "detail": detail, "dimension": measured})
    except EmbeddingError as exc:
        result.update({"ok": False, "detail": str(exc)})
    return result


def probe_llm(deep: bool = False) -> dict:
    from apps.knowledge.llm import LLMError, get_llm_provider
    from apps.settings_store.services import get_value

    provider = get_llm_provider()
    result = {
        "name": provider.name,
        "model": getattr(provider, "model", ""),
        "base_url": get_value("OPENAI_BASE_URL", ""),
    }
    if provider.name == "noop":
        result.update({"ok": True, "detail": "noop provider (nincs külső hívás)"})
        return result

    if not deep:
        ok, detail = _reachable(str(result["base_url"]))
        result.update({"ok": ok, "detail": detail})
        return result

    try:
        out = provider.generate(title="Probe", prompt="Reply with the single word: ok")
        result.update({"ok": True, "detail": f"OK: {str(out).strip()[:60]!r}"})
    except LLMError as exc:
        result.update({"ok": False, "detail": str(exc)})
    return result


def probe_vector_store() -> dict:
    from apps.embeddings.vectorstores import get_vector_store

    store = get_vector_store()
    if store.name == "local":
        return {"ok": True, "name": "local", "detail": "beépített local vector store (DB)"}
    try:
        store.ensure_collection()
        return {"ok": True, "name": store.name, "detail": "Qdrant collection elérhető"}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "name": store.name, "detail": str(exc)}


def probe_search_backend() -> dict:
    from django.db import connection

    from apps.settings_store.services import get_value

    backend = str(get_value("BRAINBOX_SEARCH_BACKEND", "simple"))
    if backend == "postgres":
        if connection.vendor != "postgresql":
            return {"ok": False, "name": backend, "detail": f"nem postgres backend: {connection.vendor}"}
        return {"ok": True, "name": backend, "detail": "PostgreSQL full-text elérhető"}
    return {"ok": True, "name": backend, "detail": "DB-független keresés"}


PROBES = {
    "embedding": probe_embedding,
    "llm": probe_llm,
    "vector": probe_vector_store,
    "search": probe_search_backend,
}


def probe_for_key(key: str, deep: bool = False) -> dict:
    """Map a setting key to the probe that validates it."""
    if key in {"BRAINBOX_EMBEDDING_PROVIDER", "BRAINBOX_EMBEDDING_MODEL", "BRAINBOX_EMBEDDING_DIM", "OPENAI_BASE_URL"}:
        return probe_embedding(deep=deep)
    if key in {"BRAINBOX_LLM_PROVIDER", "BRAINBOX_LLM_MODEL"}:
        return probe_llm(deep=deep)
    if key.startswith("QDRANT"):
        return probe_vector_store()
    if key == "BRAINBOX_SEARCH_BACKEND":
        return probe_search_backend()
    return {"ok": None, "detail": "erre a beállításra nincs automatikus teszt"}
