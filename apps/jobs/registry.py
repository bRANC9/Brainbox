"""Job registry: maps a ``task_key`` to a synchronous handler.

The engine part of this app is deliberately framework-only (registry, schedule
math, models, engine, admin) so it can be lifted into a standalone Django
package later; app-specific tasks live in ``apps/jobs/tasks_brainbox.py``.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger("brainbox.jobs")


@dataclass
class JobContext:
    """Per-run execution context handed to job handlers."""

    run: Any = None
    job: Any = None
    job_id: Any = None
    job_name: str | None = None
    task_key: str | None = None
    run_id: str | None = None
    attempt: int = 1
    config: dict = field(default_factory=dict)
    payload: dict = field(default_factory=dict)

    def log(self, message: str) -> None:
        logger.info("[job %s] %s", self.task_key, message)


JobHandler = Callable[[JobContext], Any]


@dataclass(frozen=True)
class JobSpec:
    key: str
    handler: JobHandler
    description: str = ""
    dedupe_key: str | None = None


_REGISTRY: dict[str, JobSpec] = {}


def register_job(key: str, description: str = "", dedupe_key: str | None = None):
    """Decorator registering a handler under ``key``.

    ``dedupe_key`` names a ``payload`` field; while a run with the same value is
    waiting/running, further system-triggered enqueues are skipped (prevents
    duplicate work for the same target).
    """

    def decorator(func: JobHandler) -> JobHandler:
        if key in _REGISTRY:
            raise ValueError(f"Job key '{key}' is already registered")
        _REGISTRY[key] = JobSpec(
            key=key, handler=func, description=description, dedupe_key=dedupe_key
        )
        logger.debug("Registered job '%s'", key)
        return func

    return decorator


def get_job(key: str) -> JobSpec | None:
    return _REGISTRY.get(str(key or "").strip())


def list_jobs() -> list[JobSpec]:
    return sorted(_REGISTRY.values(), key=lambda spec: spec.key)
