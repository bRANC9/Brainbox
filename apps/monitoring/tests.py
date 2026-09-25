import tempfile

from django.test import TestCase, override_settings

from apps.accounts.models import User
from apps.workspaces.services import WorkspaceService


class MonitoringTests(TestCase):
    def setUp(self):
        from apps.accounts.models import User

        self.alice = User.objects.create_superuser("root", "root@example.com", "pw")
        self.client.force_login(self.alice)

    def test_readyz_reports_ok(self):
        response = self.client.get("/readyz")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["status"], "ok")
        self.assertIn("database", response.json())

    def test_metrics_endpoint_exposes_brainbox_gauges(self):
        response = self.client.get("/metrics")
        body = response.content.decode()
        self.assertIn("brainbox_documents", body)
        self.assertIn("brainbox_audit_events_24h", body)
        # HTTP instrumentation from django-prometheus
        self.assertIn("django_http_requests", body)

    def test_metrics_respects_token(self):
        from django.test import override_settings as _override

        with _override(BRAINBOX_METRICS_TOKEN="s3cr3t"):
            response = self.client.get("/metrics")
            self.assertEqual(response.status_code, 401)
            response = self.client.get("/metrics", HTTP_AUTHORIZATION="Bearer s3cr3t")
            self.assertEqual(response.status_code, 200)


@override_settings(KNOWLEDGE_DATA_ROOT=tempfile.mkdtemp())
class RerankerTests(TestCase):
    def setUp(self):
        self.alice = User.objects.create_user("alice", "alice@example.com", "pw")
        self.workspace = WorkspaceService.create(name="Company", created_by=self.alice)

    def test_heuristic_reranker_prefers_title_match(self):
        from apps.search.rerankers import HeuristicReranker

        reranker = HeuristicReranker()
        # Same base relevance: the doc whose *title* matches must win, even
        # against an approved doc that only matches in the snippet.
        results = [
            {"title": "Networking notes", "path": "notes/net.md", "snippet": "bicep containers", "status": "approved", "score": 0.6, "updated_at": None},
            {"title": "Bicep deployment guide", "path": "guide.md", "snippet": "bicep", "status": "draft", "score": 0.6, "updated_at": None},
        ]
        ordered = reranker.rerank("bicep", results, limit=2)
        self.assertEqual(ordered[0]["title"], "Bicep deployment guide")

    def test_heuristic_reranker_keeps_high_relevance_first(self):
        from apps.search.rerankers import HeuristicReranker

        rows = [
            {"title": "x", "score": 0.9, "path": "", "snippet": "", "status": "draft", "updated_at": None},
            {"title": "y", "score": 0.1, "path": "", "snippet": "", "status": "draft", "updated_at": None},
        ]
        self.assertEqual(HeuristicReranker().rerank("q", rows)[0]["title"], "x")

    def test_heuristic_reranker_respects_limit(self):
        from apps.search.rerankers import HeuristicReranker

        rows = [{"title": f"Doc {i}", "path": f"{i}.md", "snippet": "x", "status": "draft", "score": float(i), "updated_at": None} for i in range(5)]
        self.assertEqual(len(HeuristicReranker().rerank("x", rows, limit=2)), 2)

    def test_noop_reranker_sorts_by_base_score(self):
        from apps.search.rerankers import NoopReranker

        rows = [
            {"title": "a", "score": 0.1, "path": "", "snippet": "", "status": "", "updated_at": None},
            {"title": "b", "score": 0.9, "path": "", "snippet": "", "status": "", "updated_at": None},
        ]
        self.assertEqual(NoopReranker().rerank("x", rows)[0]["title"], "b")

    def test_get_reranker_honors_setting(self):
        from apps.search import rerankers

        with override_settings(BRAINBOX_RERANKER="none"):
            self.assertIsInstance(rerankers.get_reranker(), rerankers.NoopReranker)
        with override_settings(BRAINBOX_RERANKER="heuristic"):
            self.assertIsInstance(rerankers.get_reranker(), rerankers.HeuristicReranker)

    def test_crossencoder_falls_back_when_unreachable(self):
        from apps.search import rerankers

        # No URL configured -> factory falls back to heuristic
        with override_settings(BRAINBOX_RERANKER="crossencoder", BRAINBOX_RERANK_URL=""):
            self.assertIsInstance(rerankers.get_reranker(), rerankers.HeuristicReranker)

        # Unreachable endpoint -> falls back to base score order, never raises
        bad = rerankers.CrossEncoderReranker("http://127.0.0.1:9/rerank", "m")
        rows = [{"chunk_id": "a", "score": 0.2, "title": "", "path": "", "snippet": "", "status": "", "updated_at": None}]
        self.assertEqual(bad.rerank("q", rows), rows)
