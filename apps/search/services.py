"""Hybrid search (Phase 3, terv.md 20-22).

Pipeline: candidate retrieval (full-text + vector) -> rerank -> permission
filter -> knowledge priority. Permission filtering happens *after* retrieval;
a vector hit never grants access on its own.
"""

from __future__ import annotations

from dataclasses import dataclass

from django.conf import settings
from django.db import connection
from django.db.models import Q

from apps.documents.models import Document, DocumentStatus
from apps.embeddings.models import KnowledgeChunk
from apps.embeddings.providers import EmbeddingError, get_embedding_provider
from apps.embeddings.vectorstores import VectorStoreError, get_vector_store
from apps.permissions.constants import Permission
from apps.permissions.services import PermissionService

STATUS_WEIGHTS = {
    DocumentStatus.APPROVED: 1.0,
    DocumentStatus.EXPERIMENTAL: 0.6,
    DocumentStatus.DRAFT: 0.4,
    DocumentStatus.DEPRECATED: 0.2,
    DocumentStatus.ARCHIVED: 0.1,
}


@dataclass
class Candidate:
    chunk_id: str
    document_id: str
    resource_id: str
    content: str
    text_score: float = 0.0
    semantic_score: float = 0.0


def _snippet(content: str, query: str, length: int = 260) -> str:
    text = " ".join((content or "").split())
    if not text:
        return ""
    lowered = text.lower()
    position = -1
    for term in query.lower().split():
        position = lowered.find(term)
        if position != -1:
            break
    if position == -1:
        return text[:length]
    start = max(0, position - 60)
    return ("…" if start > 0 else "") + text[start : start + length]


class TextSearchService:
    @classmethod
    def search(cls, query: str, *, filters: dict | None = None, limit: int = 20) -> list[Candidate]:
        filters = filters or {}
        if settings.BRAINBOX_SEARCH_BACKEND == "postgres" and connection.vendor == "postgresql":
            return cls._postgres(query, filters, limit)
        return cls._simple(query, filters, limit)

    @classmethod
    def _apply_filters(cls, queryset, filters):
        if filters.get("workspace_id"):
            queryset = queryset.filter(workspace_id=filters["workspace_id"])
        if filters.get("project_id"):
            queryset = queryset.filter(project_id=filters["project_id"])
        if filters.get("status"):
            queryset = queryset.filter(status__in=filters["status"])
        return queryset

    @classmethod
    def _simple(cls, query, filters, limit):
        terms = [term for term in query.lower().split() if term]
        if not terms:
            return []
        condition = Q()
        for term in terms:
            condition |= Q(content__icontains=term)
        queryset = cls._apply_filters(KnowledgeChunk.objects.filter(condition), filters)
        results: list[Candidate] = []
        for chunk in queryset.iterator(chunk_size=500):
            lowered = chunk.content.lower()
            hits = sum(1 for term in terms if term in lowered)
            if hits == 0:
                continue
            results.append(
                Candidate(
                    chunk_id=str(chunk.id),
                    document_id=str(chunk.document_id),
                    resource_id=str(chunk.resource_id),
                    content=chunk.content,
                    text_score=hits / len(terms),
                )
            )
        results.sort(key=lambda candidate: candidate.text_score, reverse=True)
        return results[:limit]

    @classmethod
    def _postgres(cls, query, filters, limit):
        from django.contrib.postgres.search import SearchQuery, SearchRank, SearchVector

        vector = SearchVector("content", config="english")
        search_query = SearchQuery(query, config="english")
        queryset = cls._apply_filters(
            KnowledgeChunk.objects.annotate(rank=SearchRank(vector, search_query)).filter(
                rank__gt=0
            ),
            filters,
        ).order_by("-rank")[:limit]
        return [
            Candidate(
                chunk_id=str(chunk.id),
                document_id=str(chunk.document_id),
                resource_id=str(chunk.resource_id),
                content=chunk.content,
                text_score=float(chunk.rank),
            )
            for chunk in queryset
        ]


class SemanticSearchService:
    @classmethod
    def search(cls, query: str, *, filters: dict | None = None, limit: int = 20) -> list[Candidate]:
        try:
            vector = get_embedding_provider().embed_one(query)
            hits = get_vector_store().search(vector, limit=limit, filters=filters or {})
        except (EmbeddingError, VectorStoreError):
            return []
        return [
            Candidate(
                chunk_id=hit.chunk_id,
                document_id=hit.document_id,
                resource_id=hit.resource_id,
                content=hit.content,
                semantic_score=max(hit.score, 0.0),
            )
            for hit in hits
        ]


class SearchService:
    MODE_WEIGHTS = {
        "text": (1.0, 0.0),
        "semantic": (0.0, 1.0),
        "hybrid": (0.5, 0.5),
    }

    @classmethod
    def search(
        cls,
        user,
        query: str,
        *,
        mode: str = "hybrid",
        workspace_id=None,
        project_id=None,
        status=None,
        limit: int = 10,
        api_key=None,
    ) -> list[dict]:
        query = (query or "").strip()
        if not query:
            return []
        mode = mode if mode in cls.MODE_WEIGHTS else "hybrid"
        text_weight, semantic_weight = cls.MODE_WEIGHTS[mode]

        filters: dict = {}
        if workspace_id:
            filters["workspace_id"] = workspace_id
        if project_id:
            filters["project_id"] = project_id
        if status:
            filters["status"] = [status] if isinstance(status, str) else list(status)

        candidate_limit = max(limit * 5, 25)
        merged: dict[str, Candidate] = {}
        if text_weight:
            for candidate in TextSearchService.search(query, filters=filters, limit=candidate_limit):
                merged.setdefault(candidate.chunk_id, candidate)
        if semantic_weight:
            for candidate in SemanticSearchService.search(
                query, filters=filters, limit=candidate_limit
            ):
                existing = merged.get(candidate.chunk_id)
                if existing is None:
                    merged[candidate.chunk_id] = candidate
                else:
                    existing.semantic_score = max(existing.semantic_score, candidate.semantic_score)

        max_text = max((c.text_score for c in merged.values()), default=0.0) or 1.0

        documents = {
            str(document.pk): document
            for document in Document.objects.select_related("workspace", "project", "resource").filter(
                pk__in=[candidate.document_id for candidate in merged.values()]
            )
        }

        scored: list[dict] = []
        seen_documents: set[str] = set()
        for candidate in merged.values():
            document = documents.get(candidate.document_id)
            if document is None:
                continue
            # Permission filtering AFTER retrieval is mandatory (terv.md 20).
            if not PermissionService.check(user, document.resource, Permission.READ, api_key=api_key):
                continue
            base = (
                text_weight * (candidate.text_score / max_text)
                + semantic_weight * candidate.semantic_score
            )
            status_weight = STATUS_WEIGHTS.get(document.status, 0.5)
            priority_boost = 1.0 + max(document.priority, 0) / 200.0
            score = base * status_weight * priority_boost
            if candidate.document_id in seen_documents:
                continue
            seen_documents.add(candidate.document_id)
            scored.append(
                {
                    "document_id": candidate.document_id,
                    "resource_id": candidate.resource_id,
                    "chunk_id": candidate.chunk_id,
                    "title": document.title,
                    "path": document.path,
                    "summary": document.summary,
                    "status": document.status,
                    "priority": document.priority,
                    "workspace": str(document.workspace_id),
                    "project": str(document.project_id) if document.project_id else None,
                    "score": round(score, 6),
                    "snippet": _snippet(candidate.content, query),
                }
            )

        scored.sort(key=lambda row: row["score"], reverse=True)
        return scored[:limit]
