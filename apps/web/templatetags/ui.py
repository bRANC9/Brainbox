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
    # link types
    "wikilink": "Wikilink",
    "reference": "Hivatkozás",
    "related": "Kapcsolódó",
    "parent": "Szülő",
    "child": "Gyerek",
    "embed": "Beágyazás",
    # resource types
    "workspace": "Munkaterület",
    "project": "Projekt",
    "folder": "Mappa",
    "document": "Dokumentum",
    "file": "Fájl",
    "git_repository": "Git-tárhely",
    "secret": "Titkos érték",
    "mcp_server": "MCP szerver",
    # missing values
    # The filter lowercases before the lookup, so the string "None" has to be
    # keyed "none" here to be found. A real Python None never reaches this map:
    # the filter returns "" for it further down.
    "none": "Nincs megjelenítve",
}


@register.filter(name="hu")
def hu(value):
    """Translate a known enum value, otherwise pass it through unchanged."""
    if value is None:
        return ""
    text = str(getattr(value, "value", value))
    return HU.get(text.lower(), str(value))


# A projekt és a hozzá tartozó fa-csomópont (DocumentFolder) két külön sor,
# és a projekt pk-ja a *Resource* id-ja, nem a csomópont saját UUID pk-ja.
# Ezért a csomópontot resource_id alapján kell megkeresni, nem pk alapján:
# a `DocumentFolder.objects.filter(pk=project.pk)` csendben semmit nem találna.
@register.simple_tag(name="node_url")
def node_url(obj):
    """Canonical ``folder_detail`` URL for a knowledge-tree node.

    Accepts a ``DocumentFolder`` (the node itself) or a ``Project``, and returns
    "" when there is no node (a project predating the tree) or the argument is
    None/unsupported - a template must never blow up over a missing link.
    """
    if obj is None:
        return ""
    # Local imports: models and the web app import each other, and a module-level
    # import here would close the templatetag <-> model cycle.
    from django.urls import reverse

    from apps.documents.models import DocumentFolder

    if isinstance(obj, DocumentFolder):
        folder = obj
    else:
        # Project.pk == Project.resource_id (resource is the pk), while the node
        # row's own pk is a different UUID: match on resource_id, never on pk.
        resource_id = getattr(obj, "resource_id", None)
        if resource_id is None:
            return ""
        folder = DocumentFolder.objects.filter(resource_id=resource_id).first()
    if folder is None:
        return ""
    try:
        return reverse(
            "web:folder_detail", args=[folder.workspace.slug, folder.tree_path()]
        )
    except Exception:
        return ""