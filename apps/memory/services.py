"""Fact extraction and the ACL-projected view.

Extraction comes in two flavours: deterministic (frontmatter keys, tags, links -
a fact is a fact, not a guess) and LLM-based (triples pulled from the content,
marked with a lower confidence). Aggregates roll several sources into one
statement, and a reflection can store a Hindsight answer as an aggregate.

The one rule across all of it: a fact is exactly as visible as its source, and an
aggregate is visible only to someone who can read *every* source it cites.
"""

from __future__ import annotations

import hashlib
import json
import re

from apps.permissions.constants import Permission
from apps.permissions.services import PermissionService

from .models import Fact, MemoryAggregate

# frontmatter key -> predicate. These are the "who is behind this" keys that make
# a document about a person, so querying that person gathers them all.
PERSON_KEYS = {
    "owner": "owner_of",
    "reports_to": "reports_to",
    "maintainer": "maintainer_of",
    "reviewer": "reviewer_of",
    "author": "author_of",
}

LLM_PROMPT = (
    "Extract factual statements from the text as a JSON array of objects with keys "
    '"subject", "predicate", "object". Subject is a person, team, system or process; '
    "predicate is a short snake_case relation; object is a value or another entity. "
    "Output ONLY the JSON array."
)


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
        """Rebuild the deterministic facts of one document."""
        from apps.tags.services import tags_for

        cls._clear(document, origin="deterministic")
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
    def extract_document_llm(cls, document) -> int:
        """Ask the LLM for facts. Best-effort: an offline provider yields none."""
        from apps.documents.services import DocumentService
        from apps.knowledge.llm import LLMError, get_llm_provider

        try:
            content = DocumentService.read_content(document)
        except Exception:  # noqa: BLE001 - a missing file is not an error here
            return 0
        if not content or not content.strip():
            return 0
        try:
            raw = get_llm_provider().generate(
                title=document.title, prompt=LLM_PROMPT, context=content[:6000]
            )
        except LLMError:
            return 0

        cls._clear(document, origin="llm")
        created = 0
        for subject, predicate, object_name in cls._parse_triples(raw):
            if cls._create(
                document, subject, predicate, object_name, origin="llm", confidence=0.7
            ):
                created += 1
        return created

    @staticmethod
    def _parse_triples(raw: str) -> list[tuple[str, str, str]]:
        text = (raw or "").strip()
        if text.startswith("```"):
            text = re.sub(r"^```[a-zA-Z]*\n?", "", text)
            text = re.sub(r"```$", "", text).strip()
        try:
            data = json.loads(text)
        except ValueError:
            return []
        if not isinstance(data, list):
            return []
        triples = []
        for row in data:
            if not isinstance(row, dict):
                continue
            subject = str(row.get("subject", "")).strip()
            predicate = str(row.get("predicate", "")).strip()
            object_name = str(row.get("object", "")).strip()
            if subject and predicate:
                triples.append((subject, predicate, object_name))
        return triples

    @classmethod
    def _clear(cls, document, *, origin: str) -> None:
        Fact.objects.filter(source_document=document, origin=origin).delete()

    @classmethod
    def _create(
        cls, document, subject, predicate, object_name, *, origin="deterministic", confidence=1.0
    ) -> bool:
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
                "origin": origin,
                "confidence": confidence,
            },
        )
        return was_created

    @classmethod
    def rebuild_all(cls, *, with_llm: bool = False) -> dict:
        from apps.documents.models import Document

        count = 0
        for document in Document.objects.select_related(
            "workspace", "project", "resource"
        ).iterator():
            count += cls.extract_document(document)
            if with_llm:
                count += cls.extract_document_llm(document)
        return {"facts": count}

    # -- the view ------------------------------------------------------------
    @classmethod
    def view(cls, user, *, subject=None, workspace=None, api_key=None) -> dict:
        """The facts and aggregates the caller may read, grouped by subject.

        Facts are filtered by their source's readability; an aggregate is kept
        only when *every* source it cites is readable. The projection never
        widens access, whatever it is asked for.
        """
        facts = list(cls._facts(subject=subject, workspace=workspace))
        readable_sources = set(
            PermissionService.allowed_resource_ids(
                user,
                [fact.source_resource_id for fact in facts],
                Permission.READ,
                api_key=api_key,
            )
        )

        aggregates = list(cls._aggregates(subject=subject, workspace=workspace))
        aggregate_source_ids = [
            source.pk for aggregate in aggregates for source in aggregate.sources.all()
        ]
        readable_aggregate_sources = set(
            PermissionService.allowed_resource_ids(
                user, aggregate_source_ids, Permission.READ, api_key=api_key
            )
        )

        groups: dict[str, dict] = {}
        for fact in facts:
            if fact.source_resource_id not in readable_sources:
                continue
            group = groups.setdefault(
                fact.subject_key,
                {"subject": fact.subject, "facts": [], "aggregates": []},
            )
            group["facts"].append(cls._fact_row(fact))
        for aggregate in aggregates:
            source_ids = [source.pk for source in aggregate.sources.all()]
            if not source_ids or not all(
                source_id in readable_aggregate_sources for source_id in source_ids
            ):
                continue
            group = groups.setdefault(
                aggregate.subject_key,
                {"subject": aggregate.subject, "facts": [], "aggregates": []},
            )
            group["aggregates"].append(
                {
                    "text": aggregate.text,
                    "origin": aggregate.origin,
                    "proof_count": aggregate.proof_count,
                    "sources": [str(source_id) for source_id in source_ids],
                }
            )
        return {
            "subjects": sorted(groups.values(), key=lambda row: row["subject"].lower())
        }

    @staticmethod
    def _facts(*, subject=None, workspace=None):
        queryset = Fact.objects.select_related(
            "source_document", "source_document__folder", "source_document__project"
        )
        if subject:
            queryset = queryset.filter(subject_key=str(subject).strip().lower())
        if workspace:
            queryset = queryset.filter(workspace_id=workspace)
        return queryset

    @staticmethod
    def _aggregates(*, subject=None, workspace=None):
        queryset = MemoryAggregate.objects.prefetch_related("sources")
        if subject:
            queryset = queryset.filter(subject_key=str(subject).strip().lower())
        if workspace:
            queryset = queryset.filter(workspace_id=workspace)
        return queryset

    @staticmethod
    def _fact_row(fact: Fact) -> dict:
        document = fact.source_document
        return {
            "predicate": fact.predicate,
            "object": fact.object,
            "assertion": fact.assertion,
            "origin": fact.origin,
            "source": {
                "document_id": str(document.pk) if document is not None else None,
                "title": document.title if document is not None else None,
                "tree_path": document.tree_path() if document is not None else None,
            },
        }


class AggregateService:
    """Consolidate a subject's facts into one evidence-backed statement.

    One aggregate per (subject, workspace): as evidence changes it is refined,
    not duplicated, and ``proof_count`` records how many sources back it. Its
    visibility is the intersection of those sources, so a belief assembled from
    several places is visible only to someone who can read all of them.
    """

    @classmethod
    def build_workspace(cls, workspace) -> int:
        facts = list(
            Fact.objects.filter(workspace=workspace).select_related("source_resource")
        )
        by_subject: dict[str, list[Fact]] = {}
        for fact in facts:
            by_subject.setdefault(fact.subject_key, []).append(fact)

        built = 0
        for subject_facts in by_subject.values():
            sources = {
                fact.source_resource_id: fact.source_resource for fact in subject_facts
            }
            if len(sources) < 2:
                # A single source is not a consolidation.
                continue
            text = "; ".join(
                sorted({fact.assertion for fact in subject_facts if fact.assertion})
            )
            cls._upsert(
                subject=subject_facts[0].subject,
                text=text,
                sources=list(sources.values()),
                workspace=workspace,
                origin="deterministic",
                confidence=1.0,
            )
            built += 1
        return built

    @classmethod
    def build_all(cls) -> dict:
        from apps.workspaces.models import Workspace

        built = 0
        for workspace in Workspace.objects.all().iterator():
            built += cls.build_workspace(workspace)
        return {"aggregates": built}

    @classmethod
    def store(
        cls, *, subject, text, sources, workspace=None, origin="reflection", confidence=0.6
    ):
        """Refine the subject's aggregate, citing the sources behind it."""
        cls._upsert(
            subject=subject,
            text=text,
            sources=sources,
            workspace=workspace,
            origin=origin,
            confidence=confidence,
        )

    @classmethod
    def _upsert(cls, *, subject, text, sources, workspace, origin, confidence) -> bool:
        subject_key = subject.strip().lower()
        source_ids = [getattr(source, "pk", source) for source in sources]
        dedupe_key = hashlib.sha256(
            f"{subject_key}|{workspace.pk if workspace is not None else ''}".encode("utf-8")
        ).hexdigest()
        aggregate, created = MemoryAggregate.objects.get_or_create(
            dedupe_key=dedupe_key,
            defaults={
                "subject": subject,
                "subject_key": subject_key,
                "text": text,
                "workspace": workspace,
                "origin": origin,
                "confidence": confidence,
                "proof_count": len(source_ids),
            },
        )
        aggregate.sources.set(source_ids)
        aggregate.text = text
        aggregate.origin = origin
        aggregate.proof_count = len(source_ids)
        aggregate.save(update_fields=["text", "origin", "proof_count", "updated_at"])
        return created


class ReflectionService:
    """Recreate the reflection step natively.

    Hindsight is an external memory service; this is the same idea built in. Take
    the facts already extracted about a subject, ask our own LLM provider for a
    grounded summary, and store it as an evidence-backed aggregate. No external
    service and no credential - and the result inherits its sources' intersection
    like every other aggregate, so a reflection can never say more than the
    caller could have read directly.
    """

    @classmethod
    def reflect(cls, subject, *, user, workspace=None, api_key=None) -> str | None:
        facts = list(FactService._facts(subject=subject, workspace=workspace))
        readable = set(
            PermissionService.allowed_resource_ids(
                user,
                [fact.source_resource_id for fact in facts],
                Permission.READ,
                api_key=api_key,
            )
        )
        usable = [fact for fact in facts if fact.source_resource_id in readable]
        if not usable:
            return None
        sources = {fact.source_resource_id: fact.source_resource for fact in usable}
        grounded = "; ".join(sorted({fact.assertion for fact in usable if fact.assertion}))
        text = cls._synthesize(usable[0].subject, grounded)
        AggregateService.store(
            subject=usable[0].subject,
            text=text,
            sources=list(sources.values()),
            workspace=usable[0].workspace,
            origin="reflection",
            confidence=0.6,
        )
        return text

    @staticmethod
    def _synthesize(subject, grounded) -> str:
        from apps.knowledge.llm import LLMError, get_llm_provider

        fallback = f"{subject}: {grounded}" if grounded else subject
        try:
            provider = get_llm_provider()
            if getattr(provider, "name", "") == "noop":
                # Offline: the deterministic consolidation is the reflection.
                return fallback
            prompt = (
                "Summarise, in two sentences and grounded ONLY in the facts below, "
                f"what is known about '{subject}'. If something is not covered, say so.\n"
                f"Facts:\n{grounded}"
            )
            text = provider.generate(title=f"About {subject}", prompt=prompt, context="")
            return text.strip() or fallback
        except LLMError:
            return fallback
