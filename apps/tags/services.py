"""Tag operations: attach/detach, folder inheritance and tag-filtering helpers."""

from __future__ import annotations

import re

from django.contrib.contenttypes.models import ContentType

from .models import Tag, TaggedItem


def normalize(name: str) -> str:
    """Normalise a tag name: lowercase, spaces/specials -> single dash."""
    raw = (name or "").strip().lower()
    raw = re.sub(r"[^a-z0-9._-]+", "-", raw)
    return raw.strip("-")


def get_or_create(name: str, user=None) -> Tag:
    clean = normalize(name)
    tag, _ = Tag.objects.get_or_create(name=clean, defaults={"created_by": user})
    return tag


def tag_target(target, names, user=None) -> list[str]:
    """Attach tags (list of names) to a model instance; returns current tags."""
    if not hasattr(target, "tagged_items"):
        raise ValueError("This object does not support tagging.")
    ctype = ContentType.objects.get_for_model(target)
    for raw in names or []:
        clean = normalize(raw)
        if not clean:
            continue
        tag = get_or_create(clean, user)
        TaggedItem.objects.get_or_create(
            tag=tag,
            content_type=ctype,
            object_id=target.pk,
            defaults={"created_by": user},
        )
    return tags_for(target)


def untag_target(target, names) -> list[str]:
    ctype = ContentType.objects.get_for_model(target)
    cleaned = {normalize(name) for name in names or [] if normalize(name)}
    TaggedItem.objects.filter(
        content_type=ctype, object_id=target.pk, tag__name__in=cleaned
    ).delete()
    return tags_for(target)


def tags_for(target) -> list[str]:
    if not hasattr(target, "tagged_items"):
        return []
    return list(target.tagged_items.values_list("tag__name", flat=True))


def all_tags() -> list[dict]:
    counts = {t.name: t.items.count() for t in Tag.objects.all()}
    return [{"name": name, "count": c} for name, c in sorted(counts.items()) if c]


# ---------------------------------------------------------------------------
# Folder inheritance
# ---------------------------------------------------------------------------
def ancestor_paths(path: str) -> list[str]:
    """`a/b/c.md` -> ['a', 'a/b'] (the folders containing the file)."""
    parts = (path or "").split("/")[:-1]
    return ["/".join(parts[: i + 1]) for i in range(len(parts))]


def effective_tags(document) -> list[str]:
    """A document's own tags plus the tags of its ancestor folders."""
    from apps.documents.models import DocumentFolder

    tags = set(tags_for(document))
    paths = ancestor_paths(document.path)
    if paths:
        for folder in DocumentFolder.objects.filter(path__in=paths):
            tags.update(tags_for(folder))
    return sorted(tags)


def filter_documents_by_tag(documents, tag_name: str):
    """Keep documents whose own tags or any ancestor folder carry the tag."""
    from apps.documents.models import DocumentFolder

    clean = normalize(tag_name)
    if not clean:
        return documents

    doc_ct = ContentType.objects.get_for_model(documents[0].__class__) if documents else None
    own_ids = set()
    if doc_ct is not None:
        own_ids = {
            str(pk)
            for pk in TaggedItem.objects.filter(
                content_type=doc_ct, tag__name=clean
            ).values_list("object_id", flat=True)
        }

    # Folders carrying the tag (matched by their DocumentFolder path).
    folder_ct = ContentType.objects.get_for_model(DocumentFolder)
    folder_paths = set()
    for item in TaggedItem.objects.filter(content_type=folder_ct, tag__name=clean):
        folder = DocumentFolder.objects.filter(pk=item.object_id).first()
        if folder is not None:
            folder_paths.add(folder.path)

    keep = []
    for doc in documents:
        if str(doc.pk) in own_ids:
            keep.append(doc)
            continue
        if any(parent in folder_paths for parent in ancestor_paths(doc.path)):
            keep.append(doc)
    return keep
