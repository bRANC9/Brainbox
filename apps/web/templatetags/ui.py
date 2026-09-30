"""UI-only translation helpers.

The API keeps the canonical English enum values (stable contract); the web UI
renders them in Hungarian through this filter, so the interface stays
consistent while the API stays machine-readable.
"""

from django import template

register = template.Library()

HU = {
    # knowledge status
    "draft": "Draft",
    "approved": "Jóváhagyott",
    "experimental": "Kísérleti",
    "deprecated": "Elavult",
    "archived": "Archivált",
    # permissions / effects
    "read": "Olvasás",
    "write": "Írás",
    "delete": "Törlés",
    "admin": "Admin",
    "use": "Használat",
    "allow": "Engedélyez",
    "deny": "Tiltás",
    # generic
    "open": "Nyitott",
    "done": "Kész",
    "snoozed": "Elhalasztott",
    "dismissed": "Elvetett",
    "yes": "igen",
    "no": "nem",
    # sources
    "web": "Web",
    "mcp": "MCP",
    "git": "Git",
    "system": "Rendszer",
    "manual": "Kézi",
    "schedule": "Ütemezett",
    "retry": "Újrapróbálkozás",
    "import": "Import",
    # job runs
    "waiting": "Várakozik",
    "running": "Fut",
    "succeeded": "Sikeres",
    "failed": "Hiba",
    "cancelled": "Megszakítva",
    "pending": "Függőben",
    # api keys / secrets
    "active": "Aktív",
    "revoked": "Visszavonva",
    "expired": "Lejárt",
    # ACL subjects
    "user": "Felhasználó",
    "group": "Csoport",
    "api_key": "API kulcs",
    # change types
    "create": "Létrehozás",
    "update": "Módosítás",
    "restore": "Visszaállítás",
}


@register.filter(name="hu")
def hu(value):
    """Translate a known enum value, otherwise pass it through unchanged."""
    if value is None:
        return ""
    text = str(getattr(value, "value", value))
    return HU.get(text.lower(), str(value))