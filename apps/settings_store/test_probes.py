from django.test import TestCase

from apps.settings_store.probes import (
    probe_embedding,
    probe_for_key,
    probe_llm,
    probe_search_backend,
)


class ProbeTests(TestCase):
    def test_deterministic_embedding_is_ok_without_network(self):
        result = probe_embedding(deep=True)
        self.assertTrue(result["ok"])
        self.assertIn("offline", result["detail"])

    def test_noop_llm_is_ok(self):
        result = probe_llm(deep=True)
        self.assertTrue(result["ok"])

    def test_probe_for_key_maps_settings(self):
        self.assertEqual(probe_for_key("BRAINBOX_EMBEDDING_MODEL")["ok"], True)
        self.assertEqual(probe_for_key("BRAINBOX_LLM_MODEL")["ok"], True)
        self.assertEqual(probe_for_key("BRAINBOX_SEARCH_BACKEND")["ok"], True)
        self.assertIsNone(probe_for_key("BRAINBOX_METRICS_TOKEN")["ok"])

    def test_unreachable_endpoint_is_reported(self):
        from unittest import mock

        from apps.settings_store.services import clear_override, set_value

        set_value(key="BRAINBOX_EMBEDDING_PROVIDER", raw="ollama", user=None)
        self.addCleanup(clear_override, "BRAINBOX_EMBEDDING_PROVIDER")
        with mock.patch("apps.settings_store.probes._reachable", return_value=(False, "nem elérhető: teszt")):
            result = probe_embedding(deep=False)
        self.assertFalse(result["ok"])
        self.assertIn("nem elérhető", result["detail"])

    def test_search_backend_probe(self):
        self.assertTrue(probe_search_backend()["ok"])
