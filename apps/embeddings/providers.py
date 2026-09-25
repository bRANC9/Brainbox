"""Embedding provider abstraction.

The default provider is dependency-free and offline (deterministic hashed
bag-of-tokens) so the platform, tests and CI work without external services.
An OpenAI-compatible provider can be enabled via BRAINBOX_EMBEDDING_PROVIDER.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import urllib.error
import urllib.request
from abc import ABC, abstractmethod

from django.conf import settings

TOKEN_RE = re.compile(r"[a-z0-9]+")


class EmbeddingError(RuntimeError):
    pass


class EmbeddingProvider(ABC):
    name = "base"

    def __init__(self, dimension: int):
        self.dimension = dimension

    @abstractmethod
    def embed(self, texts: list[str]) -> list[list[float]]:
        raise NotImplementedError

    def embed_one(self, text: str) -> list[float]:
        return self.embed([text])[0]


class DeterministicEmbeddingProvider(EmbeddingProvider):
    """Signed hash embedding: offline, stable, good enough for dev/tests."""

    name = "deterministic"

    def embed(self, texts: list[str]) -> list[list[float]]:
        return [self._embed(text) for text in texts]

    def _embed(self, text: str) -> list[float]:
        vector = [0.0] * self.dimension
        tokens = TOKEN_RE.findall((text or "").lower())
        if not tokens:
            return vector
        for index, token in enumerate(tokens):
            self._add(vector, token, 1.0)
            if index + 1 < len(tokens):
                self._add(vector, f"{token}_{tokens[index + 1]}", 0.5)
        norm = math.sqrt(sum(value * value for value in vector)) or 1.0
        return [value / norm for value in vector]

    def _add(self, vector: list[float], term: str, weight: float) -> None:
        digest = hashlib.sha256(term.encode("utf-8")).digest()
        index = int.from_bytes(digest[:4], "big") % self.dimension
        sign = 1.0 if digest[4] % 2 == 0 else -1.0
        vector[index] += sign * weight


class OpenAIEmbeddingProvider(EmbeddingProvider):
    name = "openai"

    def __init__(self, dimension: int, model: str, api_key: str, base_url: str):
        super().__init__(dimension)
        self.model = model
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")

    def embed(self, texts: list[str]) -> list[list[float]]:
        if not self.api_key:
            raise EmbeddingError("OPENAI_API_KEY is not configured.")
        payload = json.dumps({"model": self.model, "input": texts}).encode("utf-8")
        request = urllib.request.Request(
            f"{self.base_url}/embeddings",
            data=payload,
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self.api_key}",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                data = json.loads(response.read().decode("utf-8"))
        except (urllib.error.URLError, urllib.error.HTTPError) as exc:
            raise EmbeddingError(f"Embedding request failed: {exc}") from exc
        rows = sorted(data.get("data", []), key=lambda row: row.get("index", 0))
        return [row["embedding"] for row in rows]


def get_embedding_provider() -> EmbeddingProvider:
    provider = (settings.BRAINBOX_EMBEDDING_PROVIDER or "deterministic").lower()
    if provider == "openai":
        return OpenAIEmbeddingProvider(
            settings.BRAINBOX_EMBEDDING_DIM,
            settings.BRAINBOX_EMBEDDING_MODEL,
            settings.OPENAI_API_KEY,
            settings.OPENAI_BASE_URL,
        )
    return DeterministicEmbeddingProvider(settings.BRAINBOX_EMBEDDING_DIM)
