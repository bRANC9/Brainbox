"""Permission vocabulary shared across the platform.

Kept free of model imports so any app can import it without triggering the
Django app registry (avoids circular imports).
"""

from django.db import models


class Permission(models.TextChoices):
    READ = "read", "Read"
    USE = "use", "Use"
    WRITE = "write", "Write"
    DELETE = "delete", "Delete"
    ADMIN = "admin", "Admin"


class Effect(models.TextChoices):
    ALLOW = "allow", "Allow"
    DENY = "deny", "Deny"


class SubjectType(models.TextChoices):
    USER = "user", "User"
    GROUP = "group", "Group"
    API_KEY = "api_key", "API key"


# What a granted permission implies. `admin` implies everything.
IMPLIES: dict[str, set[str]] = {
    Permission.READ: {Permission.READ},
    Permission.USE: {Permission.USE},
    Permission.WRITE: {Permission.READ, Permission.WRITE},
    Permission.DELETE: {Permission.READ, Permission.WRITE, Permission.DELETE},
    Permission.ADMIN: {
        Permission.READ,
        Permission.USE,
        Permission.WRITE,
        Permission.DELETE,
        Permission.ADMIN,
    },
}


def allows(granted: str, required: str) -> bool:
    """True if holding `granted` satisfies requiring `required`."""
    return required in IMPLIES.get(granted, set())


def deny_blocks(denied: str, required: str) -> bool:
    """True if a DENY of `denied` also blocks the `required` permission.

    A deny cascades upward: denying `read` blocks `write`/`delete`/`admin`
    too, because those higher permissions subsume `read`.
    """
    if required in IMPLIES.get(denied, set()):
        return True
    return denied in IMPLIES.get(required, set())
