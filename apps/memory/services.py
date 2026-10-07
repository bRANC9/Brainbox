"""Fact extraction and the ACL-projected view.

Extraction is deterministic - frontmatter keys, tags, links - so a fact is a
fact, not a guess. The view never returns a fact whose source the caller cannot
read, and every returned fact carries its source, so nothing is asserted without
a citation.
"""

from __future__ import annotations

import hashlib

from apps.permissions.constants import Permission
from apps.permissions.services import PermissionService

from .models import Fact

# frontmatter key -> predicate. These are the "who is behind this" keys that make
# a document about a person, so querying that person gathers them all.
PERSON_KEYS = {
    "owner": "owner_of",
    "reports_to": "reports_to",
    "maintainer": "maintainer_of",
    "reviewer": "reviewer_of",
    "author": "author_of",
}


def _as_list(value) -> list[str]:
    if value is None:
        return []
    if isinstance(value, (list, tuple, set)):
        return [str(item).strip() for item in value if str(item).strip()]
    text = str(value).strip()
    return [part.strip() for part in text.split(",") if part.strip()] if text else []


class FactService:
    # -- extraction ----------------------------------------------------------
    @classmethod
    def extract_document(cls, document) -> int:
        """Rebuild the facts of one document from its frontmatter, tags, links."""
        from apps.tags.services import tags_for

        Fact.objects.filter(source_document=document).delete()
        frontmatter = document.frontmatter or {}
        title = document.title or document.path

        candidates: list[tuple[str, str, str]] = []
        for key, predicate in PERSON_KEYS.items():
            for subject in _as_list(frontmatter.get(key)):
                candidates.append((subject, predicate, title))
        for tag in tags_for(document):
            candidates.append((title, "tagged", tag))
        for link in document.resource.outgoing_links.select_related("target"):
            candidates.append((title, "links_to", link.target.name))

        created = 0
        for subject, predicate, object_name in candidates:
            if cls._create(document, subject, predicate, object_name):
                created += 1
        return created

    @classmethod
    def _create(cls, document, subject, predicate, object_name) -> bool:
        subject_key = subject.strip().lower()
        if not subject_key:
            return False
        dedupe_key = hashlib.sha256(
            f"{subject_key}|{predicate}|{object_name}|{document.resource_id}".encode("utf-8")
        ).hexdigest()
        _fact, was_created = Fact.objects.get_or_create(
            dedupe_key=dedupe_key,
            defaults={
                "subject": subject.strip(),
                "subject_key": subject_key,
                "predicate": predicate,
                "object": object_name[:512],
                "assertion": f"{subject.strip()} — {predicate} → {object_name}",
                "source_resource": document.resource,
                "source_document": document,
                "workspace": document.workspace,
                "project": document.project,
            },
        )
        return was_created

    @classmethod
    def rebuild_all(cls) -> dict:
        from apps.documents.models import Document

        count = 0
        for document in Document.objects.select_related(
            "workspace", "project", "resource"
        ).iterator():
            count += cls.extract_document(document)
        return {"facts": count}

    # -- the view ------------------------------------------------------------
    @classmethod
    def view(cls, user, *, subject=None, workspace=None, api_key=None) -> dict:
        """The facts the caller may read, grouped by subject, each cited.

        A fact is included only when its *source* is readable, so the projection
        can never widen access: the salary fact from an HR document is absent for
        anyone who cannot read that document, whatever else they can read.
        """
        queryset = Fact.objects.select_related(
            "source_document", "source_document__folder", "source_document__project"
        )
        if subject:
            queryset = queryset.filter(subject_key=str(subject).strip().lower())
        if workspace:
            queryset = queryset.filter(workspace_id=workspace)
        facts = list(queryset)

        resource_ids = [fact.source_resource_id for fact in facts]
        readable = set(
            PermissionService.allowed_resource_ids(
                user, resource_ids, Permission.READ, api_key=api_key
            )
        )
        groups: dict[str, dict] = {}
        for fact in facts:
            if fact.source_resource_id not in readable:
                continue
            group = groups.setdefault(
                fact.subject_key, {"subject": fact.subject, "facts": []}
            )
            group["facts"].append(cls._fact_row(fact))
        return {
            "subjects": sorted(groups.values(), key=lambda row: row["subject"].lower())
        }

    @staticmethod
    def _fact_row(fact: Fact) -> dict:
        document = fact.source_document
        return {
            "predicate": fact.predicate,
            "object": fact.object,
            "assertion": fact.assertion,
            "source": {
                "document_id": str(document.pk) if document is not None else None,
                "title": document.title if document is not None else None,
                "tree_path": document.tree_path() if document is not None else None,
            },
        }
