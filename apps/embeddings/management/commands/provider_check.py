"""Diagnose the configured embedding/LLM providers (works with local Ollama).

Examples:
    python manage.py provider_check
    python manage.py provider_check --text "hello"
"""

from django.core.management.base import BaseCommand


class Command(BaseCommand):
    help = "Probe the embedding and LLM providers and report reachability/dimension."

    def add_arguments(self, parser):
        parser.add_argument("--text", default="brainbox provider check")

    def handle(self, *args, **options):
        from django.conf import settings

        from apps.embeddings.providers import EmbeddingError, get_embedding_provider
        from apps.knowledge.llm import LLMError, get_llm_provider

        text = options["text"]
        self.stdout.write(f"EMBED provider={settings.BRAINBOX_EMBEDDING_PROVIDER} "
                          f"model={settings.BRAINBOX_EMBEDDING_MODEL} "
                          f"base_url={settings.OPENAI_BASE_URL} dim_cfg={settings.BRAINBOX_EMBEDDING_DIM}")
        provider = get_embedding_provider()
        try:
            vectors = provider.embed([text])
            measured = len(vectors[0])
            self.stdout.write(self.style.SUCCESS(
                f"  OK  -> provider={provider.name} model={getattr(provider, 'model', '-')} "
                f"dim_measured={measured}"
            ))
            if measured != settings.BRAINBOX_EMBEDDING_DIM:
                self.stdout.write(self.style.WARNING(
                    f"  note: measured dim {measured} != BRAINBOX_EMBEDDING_DIM "
                    f"{settings.BRAINBOX_EMBEDDING_DIM} (fine for the local store; "
                    f"update the env to keep them aligned)."
                ))
        except EmbeddingError as exc:
            self.stdout.write(self.style.ERROR(f"  FAIL -> {exc}"))
            self._hint_oauth()

        self.stdout.write(f"LLM provider={settings.BRAINBOX_LLM_PROVIDER} "
                          f"model={settings.BRAINBOX_LLM_MODEL} base_url={settings.OPENAI_BASE_URL}")
        llm = get_llm_provider()
        if llm.name == "noop":
            self.stdout.write(self.style.WARNING("  SKIP -> noop provider (no external call)"))
        else:
            try:
                out = llm.generate(title="Check", prompt="Reply with the single word: ok")
                self.stdout.write(self.style.SUCCESS(f"  OK  -> {out.strip()[:80]!r}"))
            except LLMError as exc:
                self.stdout.write(self.style.ERROR(f"  FAIL -> {exc}"))
                self._hint_oauth()

    def _hint_oauth(self):
        from django.conf import settings

        if "host.docker.internal" not in settings.OPENAI_BASE_URL and "localhost" in settings.OPENAI_BASE_URL:
            self.stdout.write(self.style.WARNING(
                "  hint: from inside Docker, 'localhost' is the container. Use "
                "http://host.docker.internal:11434/v1 for a model server on the host."
            ))
