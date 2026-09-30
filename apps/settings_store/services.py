"""Runtime settings: DB-stored overrides on top of environment defaults.

Every knob below can be changed from the web UI (or the API) without editing the
compose file. Reads go through :func:`get_value`, which prefers a stored
override and falls back to the environment value. Values are cached briefly and
invalidated on write.
"""

from __future__ import annotations

import time
from dataclasses import dataclass

from django.conf import settings as django_settings
from django.core.exceptions import ValidationError


@dataclass(frozen=True)
class SettingDefinition:
    key: str
    label: str
    category: str
    description: str = ""
    value_type: str = "string"  # string | int | bool | choice
    choices: tuple = ()
    secret: bool = False
    requires_restart: bool = False
    example: str = ""


DEFINITIONS: tuple[SettingDefinition, ...] = (
    # -- AI & embeddings ----------------------------------------------------
    SettingDefinition(
        "BRAINBOX_EMBEDDING_PROVIDER",
        "Embedding provider",
        "AI & Embeddings",
        "deterministic (offline) | ollama | openai",
        value_type="choice",
        choices=("deterministic", "ollama", "openai"),
    ),
    SettingDefinition(
        "BRAINBOX_EMBEDDING_MODEL", "Embedding model", "AI & Embeddings",
        "Ollama: nomic-embed-text, bge-m3, mxbai-embed-large",
        example="bge-m3",
    ),
    SettingDefinition(
        "BRAINBOX_EMBEDDING_DIM", "Embedding dimenzió", "AI & Embeddings",
        "A modell dimenziója (bge-m3: 1024, nomic: 768)", value_type="int",
    ),
    SettingDefinition(
        "OPENAI_BASE_URL", "OpenAI-kompatibilis endpoint", "AI & Embeddings",
        "Ollama helyben: http://host.docker.internal:11434/v1",
        example="http://host.docker.internal:11434/v1",
    ),
    SettingDefinition("OPENAI_API_KEY", "API kulcs", "AI & Embeddings",
                      "Ollama-nál üresen hagyható", secret=True),
    SettingDefinition(
        "BRAINBOX_AUTO_INDEX", "Automatikus indexelés", "AI & Embeddings",
        "Dokumentumíráskor embedding készüljön", value_type="bool",
    ),
    SettingDefinition(
        "BRAINBOX_LLM_PROVIDER", "AI draft provider", "AI & Embeddings",
        value_type="choice", choices=("noop", "ollama", "openai"),
    ),
    SettingDefinition("BRAINBOX_LLM_MODEL", "AI draft modell", "AI & Embeddings",
                      example="qwen2.5:14b"),
    SettingDefinition("BRAINBOX_STALE_DAYS", "Stale napok", "AI & Embeddings",
                      "Mekkora kor után számít elavultnak egy doku", value_type="int"),
    SettingDefinition(
        "BRAINBOX_SECRET_SCAN_MODE", "Secret scanner", "AI & Embeddings",
        value_type="choice", choices=("off", "warn", "reject"),
    ),
    # -- Search & rerank ----------------------------------------------------
    SettingDefinition("BRAINBOX_SEARCH_BACKEND", "Keresési backend", "Search",
                      value_type="choice", choices=("simple", "postgres")),
    SettingDefinition("BRAINBOX_RERANKER", "Reranker", "Search",
                      value_type="choice", choices=("heuristic", "none", "crossencoder")),
    SettingDefinition("BRAINBOX_RERANK_TOP_N", "Rerank top-N", "Search",
                      value_type="int"),
    SettingDefinition("BRAINBOX_RERANK_URL", "Rerank endpoint", "Search",
                      "OpenAI-kompatibilis /rerank"),
    SettingDefinition("BRAINBOX_RERANK_MODEL", "Rerank modell", "Search"),
    SettingDefinition("QDRANT_URL", "Qdrant URL", "Search",
                      "Üres = beépített local vector store",
                      example="http://qdrant:6333"),
    SettingDefinition("QDRANT_API_KEY", "Qdrant kulcs", "Search", secret=True),
    SettingDefinition("QDRANT_COLLECTION", "Qdrant collection", "Search"),
    SettingDefinition("BRAINBOX_CHUNK_SIZE", "Chunk méret (karakter)", "Search",
                      value_type="int"),
    SettingDefinition("BRAINBOX_CHUNK_OVERLAP", "Chunk átfedés", "Search",
                      value_type="int"),
    # -- Git ----------------------------------------------------------------
    SettingDefinition("BRAINBOX_GIT_TOKEN", "Git PAT (globális)", "Git",
                      "Privát repókhoz", secret=True),
    SettingDefinition("BRAINBOX_GITHUB_TOKEN", "GitHub token (PR)", "Git",
                      "MCP knowledge_create_pull_request-hez", secret=True),
    SettingDefinition("BRAINBOX_GIT_AUTHOR_NAME", "Git author név", "Git"),
    SettingDefinition("BRAINBOX_GIT_AUTHOR_EMAIL", "Git author e-mail", "Git"),
    # -- Jobs ---------------------------------------------------------------
    SettingDefinition("BRAINBOX_SCHEDULER_ENABLED", "Scheduler aktív", "Jobs",
                      value_type="bool"),
    SettingDefinition("BRAINBOX_SCHEDULER_TICK_SEC", "Scheduler tick (mp)", "Jobs",
                      value_type="int"),
    SettingDefinition("BRAINBOX_JOB_LOCK_TTL_SEC", "Lock TTL (mp)", "Jobs",
                      value_type="int"),
    SettingDefinition("BRAINBOX_JOB_POLL_SEC", "Worker poll (mp)", "Jobs",
                      value_type="int", requires_restart=True),
    SettingDefinition("BRAINBOX_JOB_HISTORY_DAYS", "Job history napok", "Jobs",
                      value_type="int"),
    # -- Monitoring & security ---------------------------------------------
    SettingDefinition("BRAINBOX_METRICS_TOKEN", "Metrics token", "Monitoring",
                      "Ha üres, /metrics nyitott", secret=True),
    SettingDefinition(
        "BRAINBOX_REQUIRE_EMBEDDING",
        "Embedding kötelező a readiness-ben",
        "Monitoring",
        "Ha be van kapcsolva, a /readyz 503-at ad, ha az embedding provider nem érhető el",
        value_type="bool",
    ),
)

BY_KEY = {definition.key: definition for definition in DEFINITIONS}

_CACHE_TTL_SEC = 15.0
_cache: dict = {"at": 0.0, "overrides": {}}


def invalidate_cache() -> None:
    _cache["at"] = 0.0
    _cache["overrides"] = {}


def _overrides() -> dict:
    now = time.monotonic()
    if now - _cache["at"] > _CACHE_TTL_SEC:
        from .models import RuntimeSetting

        _cache["at"] = now
        _cache["overrides"] = {row.key: row.value for row in RuntimeSetting.objects.all()}
    return _cache["overrides"]


def _coerce(definition: SettingDefinition, raw):
    if definition.value_type == "int":
        try:
            return int(str(raw).strip())
        except (TypeError, ValueError) as exc:
            raise ValidationError({definition.key: "Must be an integer."}) from exc
    if definition.value_type == "bool":
        return str(raw).strip().lower() in {"1", "true", "yes", "on"}
    if definition.value_type == "choice":
        value = str(raw).strip().lower()
        if definition.choices and value not in definition.choices:
            raise ValidationError(
                {definition.key: f"Must be one of: {', '.join(definition.choices)}"}
            )
        return value
    return "" if raw is None else str(raw)


def default_value(key: str):
    definition = BY_KEY.get(key)
    if definition is None:
        return None
    return getattr(django_settings, key, "")


def get_value(key: str, default=None):
    """Resolve a setting: stored override first, then the environment value."""
    definition = BY_KEY.get(key)
    if definition is None:
        return getattr(django_settings, key, default)
    stored = _overrides().get(key)
    if stored:
        try:
            return _coerce(definition, stored)
        except ValidationError:
            pass  # fall back to the environment value on bad stored data
    return getattr(django_settings, key, default)


def is_overridden(key: str) -> bool:
    return bool(_overrides().get(key))


def set_value(*, key: str, raw, user=None) -> None:
    definition = BY_KEY.get(key)
    if definition is None:
        raise ValidationError({key: "Unknown setting."})
    _coerce(definition, raw)  # validate
    from .models import RuntimeSetting

    RuntimeSetting.objects.update_or_create(
        key=key, defaults={"value": "" if raw is None else str(raw), "updated_by": user}
    )
    invalidate_cache()


def clear_override(key: str) -> None:
    from .models import RuntimeSetting

    RuntimeSetting.objects.filter(key=key).delete()
    invalidate_cache()


def describe() -> list[dict]:
    """UI/API view of every setting (secrets masked)."""
    rows = []
    for definition in DEFINITIONS:
        stored = _overrides().get(definition.key)
        current = get_value(definition.key)
        is_secret = definition.secret
        rows.append(
            {
                "key": definition.key,
                "label": definition.label,
                "category": definition.category,
                "description": definition.description,
                "value_type": definition.value_type,
                "choices": list(definition.choices),
                "secret": is_secret,
                "requires_restart": definition.requires_restart,
                "overridden": bool(stored),
                "current": ("***" if (is_secret and current) else current),
                "env_default": ("***" if (is_secret and default_value(definition.key)) else default_value(definition.key)),
                "example": definition.example,
            }
        )
    return rows
