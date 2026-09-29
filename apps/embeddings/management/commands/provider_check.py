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
        from apps.embeddings.providers import EmbeddingError, get_embedding_provider
        from apps.knowledge.llm import LLMError, get_llm_provider
        from apps.settings_store.services import get_value

        text = options["text"]
        self.stdout.write(
            f"EMBED provider={get_value('BRAINBOX_EMBEDDING_PROVIDER')} "
            f"model={get_value('BRAINBOX_EMBEDDING_MODEL')} "
            f"base_url={get_value('OPENAI_BASE_URL')} "
            f"dim_cfg={get_value('BRAINBOX_EMBEDDING_DIM')}"
        )
        provider = get_embedding_provider()
        try:
            vectors = provider.embed([text])
            measured = len(vectors[0])
            self.stdout.write(self.style.SUCCESS(
                f"  OK  -> provider={provider.name} model={getattr(provider, 'model', '-')} "
                f"dim_measured={measured}"
            ))
            if measured != int(get_value("BRAINBOX_EMBEDDING_DIM", 256) or 256):
                self.stdout.write(self.style.WARNING(
                    f"  note: measured dim {measured} != configured "
                    f"{get_value('BRAINBOX_EMBEDDING_DIM')} (fine for the local store; "
                    f"align it to keep things tidy)."
                ))
        except EmbeddingError as exc:
            self.stdout.write(self.style.ERROR(f"  FAIL -> {exc}"))
            self._hint_docker_host()

        self.stdout.write(
            f"LLM provider={get_value('BRAINBOX_LLM_PROVIDER')} "
            f"model={get_value('BRAINBOX_LLM_MODEL')} base_url={get_value('OPENAI_BASE_URL')}"
        )
        llm = get_llm_provider()
        if llm.name == "noop":
            self.stdout.write(self.style.WARNING("  SKIP -> noop provider (no external call)"))
        else:
            try:
                out = llm.generate(title="Check", prompt="Reply with the single word: ok")
                self.stdout.write(self.style.SUCCESS(f"  OK  -> {out.strip()[:80]!r}"))
            except LLMError as exc:
                self.stdout.write(self.style.ERROR(f"  FAIL -> {exc}"))
                self._hint_docker_host()

    def _hint_docker_host(self):
        from apps.settings_store.services import get_value

        base_url = str(get_value("OPENAI_BASE_URL", "") or "")
        if "host.docker.internal" not in base_url and "localhost" in base_url:
            self.stdout.write(self.style.WARNING(
                "  hint: from inside Docker, 'localhost' is the container. Use "
                "http://host.docker.internal:11434/v1 for a model server on the host."
            ))
