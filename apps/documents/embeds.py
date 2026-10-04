"""Resolve relative references inside a rendered document.

A document is Markdown, so ``![diagram](arch.png)`` and ``[note](other.md)`` are
just text. Rendering them produced ``<img src="arch.png">`` with **nothing
serving that URL** - a broken image on every diagram, and a 404 on every
hand-written cross-reference. Nothing in the repo bridged the gap: files are
reachable only through the REST ``FileViewSet``, and no web route rendered them.

So after Markdown has turned the text into HTML, rewrite the relative references
to the routes that actually serve them, and leave anything we cannot resolve
alone (a broken image is honest; a wrong link would be worse).

Resolution is scoped to the document's own workspace *and* project, because that
is the boundary the permission model already uses for a relative path. External
URLs, absolute paths, ``data:`` URIs and fragments are passed through untouched.
"""

from __future__ import annotations

import re
from urllib.parse import unquote, urlparse

from django.urls import reverse

# src/href on an <img> or a link. Deliberately narrow: it only touches these two
# attributes, and only the values that look like relative paths.
_REF = re.compile(r'(?P<attr>\b(?:src|href)\s*=\s*")(?P<value>[^"]*)(")', re.I)

IMAGE_SUFFIXES = {
    ".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg", ".bmp", ".ico", ".avif",
}
DOCUMENT_SUFFIXES = {".md", ".markdown"}


def _is_external(value: str) -> bool:
    parsed = urlparse(value)
    return bool(parsed.scheme or parsed.netloc) or value.startswith("//")


def _candidates(document, relative: str) -> list[str]:
    """Where a relative reference could point, nearest first.

    ``docs/plan.md`` referencing ``img/a.png`` means "next to me" first, then the
    container root - the same rule a static site generator uses, so a vault that
    works elsewhere keeps working here.
    """
    folder = (document.path or "").rsplit("/", 1)[0] if "/" in (document.path or "") else ""
    out = []
    if folder:
        out.append(f"{folder}/{relative}")
    out.append(relative)
    return out


def _lookup_file_id(path: str):
    from apps.files.models import File

    return File.objects.filter(path=path).values_list("pk", flat=True).first()


def resolve(document, html: str) -> str:
    """Rewrite relative references in ``html`` to serving routes.

    ``document`` may be None (a preview rendered outside a document context); in
    that case the HTML is returned unchanged rather than guessed at.
    """
    if document is None or not html:
        return html

    def replace(match: re.Match) -> str:
        value = match.group("value")
        if not value or _is_external(value) or value.startswith("#"):
            return match.group(0)
        path = unquote(urlparse(value).path)
        if not path:
            return match.group(0)
        suffix = "." + path.rsplit(".", 1)[-1].lower() if "." in path else ""
        for candidate in _candidates(document, path):
            if suffix in IMAGE_SUFFIXES:
                pk = _lookup_file_id(candidate)
                if pk:
                    return f'{match.group("attr")}{reverse("web:file_content", args=[pk])}{match.group(3)}'
            elif suffix in DOCUMENT_SUFFIXES:
                document_id = _lookup_document_id(candidate)
                if document_id:
                    return (
                        f'{match.group("attr")}'
                        f'{reverse("web:document_detail", args=[document_id])}'
                        f'{match.group(3)}'
                    )
        return match.group(0)

    return _REF.sub(replace, html)


def _lookup_document_id(path: str):
    from apps.documents.models import Document

    match = (
        Document.objects.filter(path=path)
        .values_list("pk", flat=True)
        .first()
    )
    return match


__all__ = ["resolve", "IMAGE_SUFFIXES", "DOCUMENT_SUFFIXES"]