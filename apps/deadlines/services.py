"""Deadline extraction from knowledge files.

Two sources, in order of confidence:

1. **Frontmatter** - explicit keys (``deadline``, ``due``, ``due_date``,
   ``target_date``, ``review_by``). Highest confidence.
2. **Inline text** - a date found near a deadline keyword
   (``deadline``, ``due``, ``határidő``, ``review by``, ``ne felejtsük el`` ...).
   Low confidence, and fenced code blocks are skipped so code samples and
   changelogs do not generate phantom deadlines.
"""

from __future__ import annotations

import re
from datetime import date, datetime

FRONTMATTER_KEYS = (
    "deadline",
    "due",
    "due_date",
    "target_date",
    "review_by",
    "review_date",
    "revisit",
)

KEYWORDS = re.compile(
    r"(deadline|due\s*date|due|target\s*date|target|review\s*by|revisit|"
    r"határidő|határidő:|legkésőbb|megjegyzés|"
    r"vissza\s+kell\s+térni|ne\s+felejtsük|follow[- ]?up)",
    re.IGNORECASE,
)

ISO_DATE = re.compile(r"\b(\d{4})-(\d{1,2})-(\d{1,2})\b")
HU_DATE = re.compile(r"\b(\d{4})\.\s?(\d{1,2})\.\s?(\d{1,2})\.?\b")
SLASH_DATE = re.compile(r"\b(\d{4})/(\d{1,2})/(\d{1,2})\b")

WINDOW_BEFORE = 60  # characters before the keyword where a date is accepted
WINDOW_AFTER = 80


def _coerce_date(value) -> date | None:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if not isinstance(value, str):
        return None
    value = value.strip()
    for pattern in (ISO_DATE, HU_DATE, SLASH_DATE):
        match = pattern.search(value)
        if match:
            try:
                return date(int(match.group(1)), int(match.group(2)), int(match.group(3)))
            except ValueError:
                return None
    return None


def _find_date(text: str) -> date | None:
    for pattern in (ISO_DATE, HU_DATE, SLASH_DATE):
        match = pattern.search(text)
        if match:
            try:
                return date(int(match.group(1)), int(match.group(2)), int(match.group(3)))
            except ValueError:
                continue
    return None


def _heading_above(lines: list[str], index: int) -> str:
    for position in range(index, -1, -1):
        stripped = lines[position].strip()
        if stripped.startswith("#"):
            return stripped.lstrip("#").strip()[:120]
    return ""


def extract_from_content(content: str) -> list[dict]:
    """Return detected deadlines as dicts (no DB writes)."""
    from apps.documents.frontmatter import parse_frontmatter

    found: list[dict] = []
    if not content:
        return found

    frontmatter, body = parse_frontmatter(content)
    for key in FRONTMATTER_KEYS:
        if key in frontmatter:
            due = _coerce_date(frontmatter.get(key))
            if due:
                found.append(
                    {
                        "title": str(frontmatter.get("title") or "").strip()[:200] or "Határidő",
                        "due_date": due,
                        "source": "frontmatter",
                        "confidence": 100,
                        "context": f"{key}: {frontmatter.get(key)}",
                        "source_ref": {"key": key},
                    }
                )

    lines = (body or "").splitlines()
    in_code = False
    for index, line in enumerate(lines):
        if line.strip().startswith("```"):
            in_code = not in_code
            continue
        if in_code:
            continue
        keyword = KEYWORDS.search(line)
        if not keyword:
            continue
        # A date must be close to the keyword on the same line, or in the
        # immediately preceding text (e.g. "2026-10-15 - deadline:").
        same_line = _find_date(line)
        due = same_line
        if due is None:
            start = max(0, keyword.start() - WINDOW_BEFORE)
            due = _find_date(line[start : keyword.start()])
        if due is None:
            after = keyword.end()
            due = _find_date(line[after : after + WINDOW_AFTER])
        if due is None and index > 0:
            due = _find_date(lines[index - 1])
        if due is None:
            continue

        heading = _heading_above(lines, index)
        title = heading or line.strip().lstrip("-*# ").strip()[:120] or "Határidő"
        found.append(
            {
                "title": title[:200],
                "due_date": due,
                "source": "inline",
                "confidence": 60,
                "context": line.strip()[:300],
                "source_ref": {"line": index + 1},
            }
        )

    return _dedupe(found)


def _dedupe(items: list[dict]) -> list[dict]:
    seen: set[tuple] = set()
    result = []
    for item in items:
        key = (item["due_date"], item["title"].strip().lower(), item["source"])
        if key in seen:
            continue
        seen.add(key)
        result.append(item)
    return result


def rebuild_deadlines(document) -> int:
    """Re-extract a document's auto deadlines (manual entries are kept)."""
    from apps.documents.services import DocumentService

    from .models import DeadlineSource, DeadlineStatus, KnowledgeDeadline

    content = DocumentService.read_content(document)
    KnowledgeDeadline.objects.filter(
        document=document, source__in=[DeadlineSource.FRONTMATTER, DeadlineSource.INLINE]
    ).delete()
    items = extract_from_content(content)
    for item in items:
        KnowledgeDeadline.objects.get_or_create(
            document=document,
            due_date=item["due_date"],
            title=item["title"],
            source=item["source"],
            defaults={
                "resource": document.resource,
                "workspace": document.workspace,
                "project": document.project,
                "status": DeadlineStatus.OPEN,
                "confidence": item["confidence"],
                "context": item["context"],
                "source_ref": item["source_ref"],
            },
        )
    return len(items)
