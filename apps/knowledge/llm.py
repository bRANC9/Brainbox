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
    """Any OpenAI-compatible ``/chat/completions`` endpoint.

    Also used for a local Ollama server
    (``OPENAI_BASE_URL=http://host.docker.internal:11434/v1``); Ollama ignores
    the API key, so ``OPENAI_API_KEY`` may be empty.
    """

    name = "openai"

    def __init__(self, api_key: str, base_url: str, model: str):
        self.api_key = api_key or ""
        self.base_url = base_url.rstrip("/")
        self.model = model

    def generate(self, *, title: str, prompt: str, context: str = "") -> str:
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
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        request = urllib.request.Request(
            f"{self.base_url}/chat/completions",
            data=payload,
            headers=headers,
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=120) as response:
                data = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            detail = ""
            try:
                detail = exc.read().decode("utf-8", errors="replace")[:300]
            except Exception:  # noqa: BLE001
                pass
            raise LLMError(f"LLM request failed ({exc.code}): {detail}") from exc
        except (urllib.error.URLError, TimeoutError, ValueError, KeyError) as exc:
            raise LLMError(
                f"LLM endpoint '{self.base_url}' unreachable: {exc}. "
                "If it runs on the Docker host, use host.docker.internal, not localhost."
            ) from exc
        return data["choices"][0]["message"]["content"]


def get_llm_provider() -> LLMProvider:
    provider = (getattr(settings, "BRAINBOX_LLM_PROVIDER", "noop") or "noop").lower()
    if provider in {"openai", "ollama", "openai-compatible"}:
        return OpenAILLMProvider(
            settings.OPENAI_API_KEY,
            settings.OPENAI_BASE_URL,
            getattr(settings, "BRAINBOX_LLM_MODEL", "gpt-4o-mini"),
        )
    return NoopLLMProvider()
