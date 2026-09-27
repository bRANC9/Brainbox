import os
import subprocess
import tempfile
from pathlib import Path

from django.test import TestCase, override_settings

from apps.accounts.models import User
from apps.documents.models import Document, DocumentStatus
from apps.documents.services import DocumentService
from apps.files.models import File
from apps.git.services import GitService
from apps.links.models import ResourceLink
from apps.workspaces.services import WorkspaceService


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        check=True,
        capture_output=True,
        text=True,
        env={**os.environ, "GIT_TERMINAL_PROMPT": "0"},
    )


def _commit_remote(path: Path, message: str) -> None:
    _git(path, "add", "-A")
    _git(
        path,
        "-c",
        "user.name=Test",
        "-c",
        "user.email=test@example.com",
        "commit",
        "-m",
        message,
    )


class AuthUrlStyleTests(TestCase):
    """Azure DevOps needs Basic auth, GitHub needs the x-access-token form."""

    def test_github_uses_x_access_token(self):
        from apps.git.git_cli import authenticated_url

        url = authenticated_url("https://github.com/acme/knowledge.git", "ghp_x")
        self.assertTrue(url.startswith("https://x-access-token:ghp_x@github.com/"))

    def test_azure_devops_uses_basic_auth(self):
        from apps.git.git_cli import authenticated_url

        url = authenticated_url("https://dev.azure.com/acme/Infra/_git/ops", "pat123")
        self.assertIn("@dev.azure.com", url)
        self.assertIn("pat123@", url)
        self.assertNotIn("x-access-token", url)

    def test_legacy_visualstudio_com_host(self):
        from apps.git.git_cli import authenticated_url

        url = authenticated_url("https://acme.visualstudio.com/Infra/_git/ops", "pat123")
        self.assertIn("@acme.visualstudio.com", url)
        self.assertNotIn("x-access-token", url)

    def test_style_can_be_forced(self):
        from apps.git.git_cli import authenticated_url

        url = authenticated_url(
            "https://git.example.com/acme/ops.git", "tok", style="basic", username="ci"
        )
        self.assertIn("ci:tok@git.example.com", url)

    def test_gitea_keeps_github_style_by_default(self):
        from apps.git.git_cli import authenticated_url

        url = authenticated_url("https://git.true.local:3000/acme/ops.git", "tok")
        self.assertIn("x-access-token:tok@git.true.local", url)

    def test_no_token_leaves_url_untouched(self):
        from apps.git.git_cli import authenticated_url

        for url in ("https://github.com/a/b.git", "git@github.com:a/b.git", "ssh://git@host/a.git"):
            self.assertEqual(authenticated_url(url, ""), url)
            self.assertEqual(authenticated_url(url, None), url)


@override_settings(KNOWLEDGE_DATA_ROOT=tempfile.mkdtemp())
class GitImportTests(TestCase):
    def setUp(self):
        self.work = Path(tempfile.mkdtemp())
        self.remote = self._make_remote(self.work / "vault")
        self.user = User.objects.create_user("alice", "alice@example.com", "pw")
        self.workspace = WorkspaceService.create(name="Company", created_by=self.user)

    def _make_remote(self, path: Path) -> Path:
        path.mkdir(parents=True)
        (path / "Skills").mkdir()
        (path / "Skills" / "azure.md").write_text(
            "---\ntype: skill\nstatus: approved\npriority: 100\n---\n"
            "# Azure\n\nSee [[bicep]] for details.\n",
            encoding="utf-8",
        )
        (path / "bicep.md").write_text("# Bicep\n\nContent\n", encoding="utf-8")
        (path / "logo.png").write_bytes(b"\x89PNG\r\n\x1a\n fake")
        _git(path, "init", "-b", "main")
        _commit_remote(path, "init")
        return path

    def test_attach_clones_and_imports_obsidian_vault(self):
        repository = GitService.attach_repository(
            workspace=self.workspace,
            remote_url=str(self.remote),
            name="vault",
            created_by=self.user,
        )

        self.assertTrue(repository.directory.exists())
        self.assertEqual(Document.objects.filter(workspace=self.workspace).count(), 2)
        self.assertEqual(File.objects.filter(workspace=self.workspace).count(), 1)

        azure = Document.objects.get(path="Skills/azure.md")
        self.assertEqual(azure.status, DocumentStatus.APPROVED)
        self.assertEqual(azure.priority, 100)
        self.assertIn("for details", DocumentService.read_content(azure))

        # The storage resolver must use the repo checkout, not the local layout.
        self.assertTrue(DocumentService.storage_path(azure).is_relative_to(repository.directory))

        bicep = Document.objects.get(path="bicep.md")
        link = ResourceLink.objects.filter(source=azure.resource, target=bicep.resource).first()
        self.assertIsNotNone(link)

    def test_pull_imports_new_remote_documents(self):
        repository = GitService.attach_repository(
            workspace=self.workspace, remote_url=str(self.remote), created_by=self.user
        )

        (self.remote / "new-note.md").write_text("# New note\n", encoding="utf-8")
        _commit_remote(self.remote, "add note")

        result = GitService.pull_repository(repository, user=self.user)

        self.assertTrue(Document.objects.filter(workspace=self.workspace, path="new-note.md").exists())
        self.assertGreaterEqual(result["documents_created"], 1)

    def test_platform_edit_is_committed_back_to_git(self):
        repository = GitService.attach_repository(
            workspace=self.workspace, remote_url=str(self.remote), created_by=self.user
        )
        document = Document.objects.get(path="bicep.md")
        client = GitService._client(repository)
        before = client.head_sha()

        DocumentService.update_content(
            document=document, content="# Bicep\n\nupdated from platform\n", user=self.user
        )

        after = client.head_sha()
        self.assertNotEqual(before, after)
        self.assertIn(
            "updated from platform",
            (repository.directory / "bicep.md").read_text(encoding="utf-8"),
        )

    def test_token_is_not_persisted_in_git_config(self):
        from django.test import override_settings

        with override_settings(BRAINBOX_GIT_TOKEN="supersecrettoken"):
            repository = GitService.attach_repository(
                workspace=self.workspace, remote_url=str(self.remote), created_by=self.user
            )
        config = (repository.directory / ".git" / "config").read_text(encoding="utf-8")
        self.assertNotIn("supersecrettoken", config)
        # The clean remote is stored (git escapes path separators on Windows).
        self.assertIn(self.remote.name, config)

    def test_per_repository_secret_credential(self):
        from apps.secrets.models import SecretType
        from apps.secrets.services import SecretService

        secret = SecretService.create(
            owner=self.user,
            name="repo-token",
            secret_type=SecretType.BEARER_TOKEN,
            payload="ghp_repotoken123",
        )
        SecretService.attach(secret=secret, workspace=self.workspace)
        repository = GitService.attach_repository(
            workspace=self.workspace,
            remote_url=str(self.remote),
            secret=secret,
            created_by=self.user,
        )
        self.assertEqual(repository.secret_id, secret.pk)
        self.assertEqual(GitService._auth_token(repository), "ghp_repotoken123")
        # config must stay clean even with a per-repo secret
        config = (repository.directory / ".git" / "config").read_text(encoding="utf-8")
        self.assertNotIn("ghp_repotoken123", config)
