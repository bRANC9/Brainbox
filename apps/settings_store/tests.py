from django.test import TestCase, override_settings

from apps.accounts.models import User
from apps.settings_store.models import RuntimeSetting
from apps.settings_store.services import (
    BY_KEY,
    clear_override,
    describe,
    get_value,
    set_value,
)


@override_settings(BRAINBOX_LLM_MODEL="env-model", BRAINBOX_EMBEDDING_MODEL="env-embed")
class RuntimeSettingsTests(TestCase):
    def setUp(self):
        self.superuser = User.objects.create_superuser("root", "root@example.com", "pw")
        self.normal = User.objects.create_user("bob", "bob@example.com", "pw")
        self.addCleanup(clear_override, "BRAINBOX_LLM_MODEL")
        self.addCleanup(clear_override, "BRAINBOX_EMBEDDING_DIM")
        self.addCleanup(clear_override, "OPENAI_API_KEY")

    def test_env_default_used(self):
        self.assertEqual(get_value("BRAINBOX_LLM_MODEL"), "env-model")

    def test_override_wins_and_clear_restores(self):
        set_value(key="BRAINBOX_LLM_MODEL", raw="db-model", user=self.superuser)
        self.assertEqual(get_value("BRAINBOX_LLM_MODEL"), "db-model")
        self.assertTrue(RuntimeSetting.objects.filter(key="BRAINBOX_LLM_MODEL").exists())
        clear_override("BRAINBOX_LLM_MODEL")
        self.assertEqual(get_value("BRAINBOX_LLM_MODEL"), "env-model")

    def test_type_coercion(self):
        set_value(key="BRAINBOX_EMBEDDING_DIM", raw="1024", user=self.superuser)
        self.assertEqual(get_value("BRAINBOX_EMBEDDING_DIM"), 1024)

    def test_invalid_int_rejected(self):
        from django.core.exceptions import ValidationError

        with self.assertRaises(ValidationError):
            set_value(key="BRAINBOX_EMBEDDING_DIM", raw="not-an-int", user=self.superuser)

    def test_choice_validated(self):
        from django.core.exceptions import ValidationError

        with self.assertRaises(ValidationError):
            set_value(key="BRAINBOX_LLM_PROVIDER", raw="bogus", user=self.superuser)
        set_value(key="BRAINBOX_LLM_PROVIDER", raw="ollama", user=self.superuser)
        self.assertEqual(get_value("BRAINBOX_LLM_PROVIDER"), "ollama")
        self.addCleanup(clear_override, "BRAINBOX_LLM_PROVIDER")

    def test_secrets_masked_in_describe(self):
        set_value(key="OPENAI_API_KEY", raw="sk-secret", user=self.superuser)
        rows = {row["key"]: row for row in describe()}
        self.assertEqual(rows["OPENAI_API_KEY"]["current"], "***")
        self.assertTrue(rows["OPENAI_API_KEY"]["overridden"])

    def test_providers_follow_overrides(self):
        from apps.embeddings.providers import get_embedding_provider
        from apps.knowledge.llm import get_llm_provider

        set_value(key="BRAINBOX_LLM_PROVIDER", raw="ollama", user=self.superuser)
        set_value(key="BRAINBOX_LLM_MODEL", raw="qwen2.5:14b", user=self.superuser)
        llm = get_llm_provider()
        self.assertEqual(llm.name, "openai")
        self.assertEqual(llm.model, "qwen2.5:14b")
        self.addCleanup(clear_override, "BRAINBOX_LLM_PROVIDER")

        set_value(key="BRAINBOX_EMBEDDING_PROVIDER", raw="ollama", user=self.superuser)
        set_value(key="BRAINBOX_EMBEDDING_MODEL", raw="bge-m3", user=self.superuser)
        provider = get_embedding_provider()
        self.assertEqual(provider.name, "openai")
        self.assertEqual(provider.model, "bge-m3")
        self.addCleanup(clear_override, "BRAINBOX_EMBEDDING_PROVIDER")
        self.addCleanup(clear_override, "BRAINBOX_EMBEDDING_MODEL")

    def test_registry_covers_ai_settings(self):
        for key in (
            "BRAINBOX_EMBEDDING_PROVIDER",
            "BRAINBOX_EMBEDDING_MODEL",
            "OPENAI_BASE_URL",
            "BRAINBOX_LLM_PROVIDER",
            "BRAINBOX_LLM_MODEL",
        ):
            self.assertIn(key, BY_KEY)

    def test_unknown_key_rejected(self):
        from django.core.exceptions import ValidationError

        with self.assertRaises(ValidationError):
            set_value(key="NOT_A_SETTING", raw="x", user=self.superuser)


class SettingsApiTests(TestCase):
    def setUp(self):
        self.superuser = User.objects.create_superuser("root", "root@example.com", "pw")
        self.normal = User.objects.create_user("bob", "bob@example.com", "pw")
        self.addCleanup(clear_override, "BRAINBOX_LLM_MODEL")

    def test_only_superuser_can_read_settings_api(self):
        from rest_framework.test import APIClient

        client = APIClient()
        client.force_authenticate(self.normal)
        self.assertIn(client.get("/api/v1/settings/").status_code, (401, 403))
        client.force_authenticate(self.superuser)
        self.assertEqual(client.get("/api/v1/settings/").status_code, 200)

    def test_superuser_can_patch_setting(self):
        from rest_framework.test import APIClient

        client = APIClient()
        client.force_authenticate(self.superuser)
        response = client.patch(
            "/api/v1/settings/BRAINBOX_LLM_MODEL/", {"value": "qwen3"}, format="json"
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(get_value("BRAINBOX_LLM_MODEL"), "qwen3")
