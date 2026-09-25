"""Reranking abstraction (terv.md 20 - "reranking" step of the search pipeline).

The reranker runs *after* candidate retrieval and *after* permission filtering,
on the top-N candidates, so it can never surface an inaccessible document.

Implementations:
- ``none``        - pass-through (base scores only)
- ``heuristic``   - deterministic feature blend (default, no external service):
                    title/path match, exact phrase, knowledge status, freshness
- ``crossencoder``- OpenAI-compatible ``/rerank`` endpoint (optional)
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from abc import ABC, abstractmethod

from django.conf import settings
from django.utils import timezone

STATUS_SCORES = {
    "approved": 1.0,
    "experimental": 0.6,
    "draft": 0.4,
    "deprecated": 0.2,
    "archived": 0.1,
}


class Reranker(ABC):
    name = "base"

    @abstractmethod
    def rerank(self, query: str, results: list[dict], *, limit: int | None = None) -> list[dict]:
        raise NotImplementedError


class NoopReranker(Reranker):
    name = "none"

    def rerank(self, query: str, results: list[dict], *, limit: int | None = None) -> list[dict]:
        ordered = sorted(results, key=lambda row: row.get("score", 0.0), reverse=True)
        return ordered[:limit] if limit else ordered


class HeuristicReranker(Reranker):
    """Offline, deterministic reranking - no model, no latency."""

    name = "heuristic"
    weights = {
        "title": 0.35,
        "path": 0.15,
        "phrase": 0.20,
        "status": 0.20,
        "freshness": 0.10,
    }

    def rerank(self, query: str, results: list[dict], *, limit: int | None = None) -> list[dict]:
        terms = [term for term in (query or "").lower().split() if term]
        now = timezone.now()
        scored: list[dict] = []

        for row in results:
            base = float(row.get("score") or 0.0)
            title = (row.get("title") or "").lower()
            path = (row.get("path") or "").lower().replace("/", " ")
            snippet = (row.get("snippet") or "").lower()

            if terms and all(term in title for term in terms):
                title_hit = 1.0
            elif terms and any(term in title for term in terms):
                title_hit = 0.5
            else:
                title_hit = 0.0

            path_hit = 1.0 if terms and any(term in path for term in terms) else 0.0
            phrase = 1.0 if query and query.strip().lower() in snippet else 0.0
            status = STATUS_SCORES.get(row.get("status"), 0.5)
            freshness = self._freshness(row.get("updated_at"), now)

            boost = 1.0 + (
                self.weights["title"] * title_hit
                + self.weights["path"] * path_hit
                + self.weights["phrase"] * phrase
                + self.weights["status"] * status
                + self.weights["freshness"] * freshness
            )
            enriched = dict(row)
            enriched["rerank_score"] = round(base * boost, 6)
            scored.append(enriched)

        scored.sort(
            key=lambda item: (item["rerank_score"], float(item.get("score") or 0.0)),
            reverse=True,
        )
        return scored[:limit] if limit else scored

    @staticmethod
    def _freshness(updated_at, now) -> float:
        if not updated_at:
            return 0.0
        try:
            from datetime import datetime

            if isinstance(updated_at, str):
                updated_at = datetime.fromisoformat(updated_at)
            days = max((now - updated_at).days, 0)
        except Exception:  # noqa: BLE001 - missing/unparsable timestamp
            return 0.0
        return 1.0 / (1.0 + days / 180.0)


class CrossEncoderReranker(Reranker):
    """Optional OpenAI-compatible ``/rerank`` endpoint."""

    name = "crossencoder"

    def __init__(self, url: str, model: str):
        if not url:
            raise ValueError("BRAINBOX_RERANK_URL is required for the crossencoder reranker.")
        self.url = url.rstrip("/")
        self.model = model

    def rerank(self, query: str, results: list[dict], *, limit: int | None = None) -> list[dict]:
        if not results:
            return []
        documents = [
            {
                "id": row.get("chunk_id") or row.get("document_id"),
                "text": f"{row.get('title', '')}\n{row.get('snippet', '')}",
            }
            for row in results
        ]
        payload = json.dumps(
            {"model": self.model, "query": query, "documents": documents, "top_n": limit or len(documents)}
        ).encode("utf-8")
        request = urllib.request.Request(
            self.url, data=payload, headers={"Content-Type": "application/json"}, method="POST"
        )
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                data = json.loads(response.read().decode("utf-8"))
        except (urllib.error.URLError, urllib.error.HTTPError, ValueError):
            # Never break search because the reranker is unavailable.
            return NoopReranker().rerank(query, results, limit=limit)

        scores = {
            str(item.get("id")): float(item.get("relevance_score", 0.0))
            for item in data.get("results", [])
        }
        reranked = []
        for row in results:
            key = str(row.get("chunk_id") or row.get("document_id"))
            enriched = dict(row)
            enriched["rerank_score"] = scores.get(key, 0.0)
            reranked.append(enriched)
        reranked.sort(key=lambda item: item["rerank_score"], reverse=True)
        return reranked[:limit] if limit else reranked


def get_reranker() -> Reranker:
    name = (settings.BRAINBOX_RERANKER or "heuristic").lower()
    if name == "none":
        return NoopReranker()
    if name in {"crossencoder", "cross-encoder"}:
        try:
            return CrossEncoderReranker(
                settings.BRAINBOX_RERANK_URL, settings.BRAINBOX_RERANK_MODEL
            )
        except ValueError:
            return HeuristicReranker()
    return HeuristicReranker()
