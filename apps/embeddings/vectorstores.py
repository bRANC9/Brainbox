"""Vector store abstraction.

``LocalVectorStore`` keeps embeddings in PostgreSQL (JSON) and scores in
Python, so the platform works without external services. ``QdrantVectorStore``
talks to Qdrant over its REST API (no extra Python dependency).
"""

from __future__ import annotations

import json
import math
import urllib.error
import urllib.request
from abc import ABC, abstractmethod
from dataclasses import dataclass, field

from django.conf import settings

from .models import KnowledgeChunk


class VectorStoreError(RuntimeError):
    pass


@dataclass
class VectorHit:
    document_id: str
    resource_id: str
    chunk_id: str
    content: str
    score: float
    payload: dict = field(default_factory=dict)


def _cosine(a: list[float], b: list[float]) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b, strict=False))
    norm_a = math.sqrt(sum(x * x for x in a))
    norm_b = math.sqrt(sum(y * y for y in b))
    if norm_a == 0.0 or norm_b == 0.0:
        return 0.0
    return dot / (norm_a * norm_b)


class VectorStore(ABC):
    name = "base"

    @abstractmethod
    def upsert(self, chunks: list[KnowledgeChunk]) -> None:
        raise NotImplementedError

    @abstractmethod
    def delete_document(self, document_id) -> None:
        raise NotImplementedError

    @abstractmethod
    def search(self, vector: list[float], *, limit: int, filters: dict | None = None) -> list[VectorHit]:
        raise NotImplementedError


class LocalVectorStore(VectorStore):
    name = "local"

    def upsert(self, chunks: list[KnowledgeChunk]) -> None:
        # Embeddings are stored on the chunk rows themselves.
        return None

    def delete_document(self, document_id) -> None:
        # Chunk rows are managed by IndexingService; nothing external to clean.
        return None

    def search(self, vector, *, limit=10, filters=None) -> list[VectorHit]:
        filters = filters or {}
        queryset = KnowledgeChunk.objects.exclude(embedding__isnull=True)
        if filters.get("workspace_id"):
            queryset = queryset.filter(workspace_id=filters["workspace_id"])
        if filters.get("project_id"):
            queryset = queryset.filter(project_id=filters["project_id"])
        if filters.get("status"):
            queryset = queryset.filter(status__in=filters["status"])

        hits: list[VectorHit] = []
        for chunk in queryset.iterator(chunk_size=500):
            score = _cosine(vector, chunk.embedding or [])
            if score <= 0:
                continue
            hits.append(
                VectorHit(
                    document_id=str(chunk.document_id),
                    resource_id=str(chunk.resource_id),
                    chunk_id=str(chunk.id),
                    content=chunk.content,
                    score=score,
                    payload={"chunk_index": chunk.chunk_index, "status": chunk.status},
                )
            )
        hits.sort(key=lambda hit: hit.score, reverse=True)
        return hits[:limit]


class QdrantVectorStore(VectorStore):
    name = "qdrant"

    def __init__(self, url: str, api_key: str, collection: str, dimension: int):
        self.url = url.rstrip("/")
        self.api_key = api_key
        self.collection = collection
        self.dimension = dimension

    # -- REST helpers --------------------------------------------------------
    def _request(self, method: str, path: str, body: dict | None = None) -> dict:
        data = json.dumps(body).encode("utf-8") if body is not None else None
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["api-key"] = self.api_key
        request = urllib.request.Request(
            f"{self.url}{path}", data=data, headers=headers, method=method
        )
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                raw = response.read().decode("utf-8")
                return json.loads(raw) if raw else {}
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                return {}
            raise VectorStoreError(f"Qdrant {method} {path} failed: {exc}") from exc
        except urllib.error.URLError as exc:
            raise VectorStoreError(f"Qdrant unreachable: {exc}") from exc

    def ensure_collection(self, dimension: int | None = None) -> None:
        existing = self._request("GET", f"/collections/{self.collection}")
        if existing.get("result"):
            return
        size = int(dimension or self.dimension or 0)
        if not size:
            raise VectorStoreError(
                "Cannot create the Qdrant collection without a vector size; "
                "index at least one document first."
            )
        self._request(
            "PUT",
            f"/collections/{self.collection}",
            {"vectors": {"size": size, "distance": "Cosine"}},
        )

    # -- VectorStore ---------------------------------------------------------
    def upsert(self, chunks) -> None:
        if not chunks:
            return
        points = []
        for chunk in chunks:
            if not chunk.embedding:
                continue
            points.append(
                {
                    "id": str(chunk.id),
                    "vector": chunk.embedding,
                    "payload": {
                        "document_id": str(chunk.document_id),
                        "resource_id": str(chunk.resource_id),
                        "workspace_id": str(chunk.workspace_id),
                        "project_id": str(chunk.project_id) if chunk.project_id else None,
                        "chunk_index": chunk.chunk_index,
                        "content": chunk.content,
                        "status": chunk.status,
                        "priority": chunk.priority,
                    },
                }
            )
        if not points:
            return
        # Size the collection from the vectors themselves: local Ollama
        # embedding models return e.g. 768 or 1024 dims, which rarely matches
        # the configured BRAINBOX_EMBEDDING_DIM default.
        self.ensure_collection(dimension=len(points[0]["vector"]))
        self._request(
            "PUT", f"/collections/{self.collection}/points?wait=true", {"points": points}
        )

    def delete_document(self, document_id) -> None:
        self._request(
            "POST",
            f"/collections/{self.collection}/points/delete?wait=true",
            {"filter": {"must": [{"key": "document_id", "match": {"value": str(document_id)}}]}},
        )

    def search(self, vector, *, limit=10, filters=None) -> list[VectorHit]:
        filters = filters or {}
        must = []
        if filters.get("workspace_id"):
            must.append({"key": "workspace_id", "match": {"value": str(filters["workspace_id"])}})
        if filters.get("project_id"):
            must.append({"key": "project_id", "match": {"value": str(filters["project_id"])}})
        if filters.get("status"):
            must.append({"key": "status", "match": {"any": list(filters["status"])}})
        body = {"vector": vector, "limit": limit, "with_payload": True}
        if must:
            body["filter"] = {"must": must}
        result = self._request(
            "POST", f"/collections/{self.collection}/points/search", body
        )
        hits = []
        for row in result.get("result", []):
            payload = row.get("payload", {})
            hits.append(
                VectorHit(
                    document_id=payload.get("document_id", ""),
                    resource_id=payload.get("resource_id", ""),
                    chunk_id=str(row.get("id", "")),
                    content=payload.get("content", ""),
                    score=float(row.get("score", 0.0)),
                    payload=payload,
                )
            )
        return hits


def get_vector_store() -> VectorStore:
    if settings.QDRANT_URL:
        from .providers import get_embedding_provider

        return QdrantVectorStore(
            settings.QDRANT_URL,
            settings.QDRANT_API_KEY,
            settings.QDRANT_COLLECTION,
            get_embedding_provider().dimension,
        )
    return LocalVectorStore()
