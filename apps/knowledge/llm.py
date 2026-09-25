"""LLM provider abstraction for AI draft generation (Phase 7).

Default is a deterministic, offline provider that produces a structured draft
skeleton. An OpenAI-compatible provider can be enabled for real generation.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from abc import ABC, abstractmethod

from django.conf import settings


class LLMError(RuntimeError):
    pass


class LLMProvider(ABC):
    name = "base"

    @abstractmethod
    def generate(self, *, title: str, prompt: str, context: str = "") -> str:
        raise NotImplementedError


class NoopLLMProvider(LLMProvider):
    """Offline placeholder: creates a reviewable skeleton draft."""

    name = "noop"

    def generate(self, *, title: str, prompt: str, context: str = "") -> str:
        lines = [
            "---",
            "status: draft",
            "ai_generated: true",
            "---",
            f"# {title}",
            "",
            "## Summary",
            f"> Requested: {prompt.strip()}",
            "",
            "## Context",
            context.strip() or "_No related company knowledge found._",
            "",
            "## Proposed approach",
            "- TODO",
            "",
            "## Review checklist",
            "- [ ] Fact-checked against company conventions",
            "- [ ] Owner assigned",
            "- [ ] Approved before use",
            "",
        ]
        return "\n".join(lines)


class OpenAILLMProvider(LLMProvider):
    name = "openai"

    def __init__(self, api_key: str, base_url: str, model: str):
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.model = model

    def generate(self, *, title: str, prompt: str, context: str = "") -> str:
        if not self.api_key:
            raise LLMError("OPENAI_API_KEY is not configured.")
        system = (
            "You draft engineering knowledge for an internal platform. "
            "Return concise Markdown that follows company conventions when provided."
        )
        user = f"Title: {title}\n\nTask:\n{prompt}"
        if context:
            user += f"\n\nRelevant company knowledge:\n{context[:6000]}"
        payload = json.dumps(
            {
                "model": self.model,
                "messages": [
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
            }
        ).encode("utf-8")
        request = urllib.request.Request(
            f"{self.base_url}/chat/completions",
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
        except (urllib.error.URLError, urllib.error.HTTPError, ValueError) as exc:
            raise LLMError(f"LLM request failed: {exc}") from exc
        return data["choices"][0]["message"]["content"]


def get_llm_provider() -> LLMProvider:
    provider = (getattr(settings, "BRAINBOX_LLM_PROVIDER", "noop") or "noop").lower()
    if provider == "openai":
        return OpenAILLMProvider(
            settings.OPENAI_API_KEY,
            settings.OPENAI_BASE_URL,
            getattr(settings, "BRAINBOX_LLM_MODEL", "gpt-4o-mini"),
        )
    return NoopLLMProvider()
