"""Thin subprocess wrapper around the `git` CLI.

We shell out instead of adding a Python Git dependency so the container only
needs the `git` binary (already installed in the Dockerfile) and so credential
handling stays explicit.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit


class GitError(RuntimeError):
    pass


def authenticated_url(url: str, token: str | None) -> str:
    """Inject an `x-access-token` credential into an HTTPS remote URL."""
    if not token or not url.startswith("https://"):
        return url
    parts = urlsplit(url)
    if parts.username:
        return url
    netloc = f"x-access-token:{token}@{parts.hostname}"
    if parts.port:
        netloc = f"{netloc}:{parts.port}"
    return urlunsplit((parts.scheme, netloc, parts.path, parts.query, parts.fragment))


class GitClient:
    def __init__(self, path: str | Path, timeout: int = 120):
        self.path = Path(path)
        self.timeout = timeout

    # -- low level -----------------------------------------------------------
    def run(
        self,
        *args: str,
        cwd: str | Path | None = None,
        check: bool = True,
        env: dict | None = None,
    ) -> subprocess.CompletedProcess:
        workdir = Path(cwd) if cwd is not None else self.path
        command = ["git", *args]
        run_env = {
            "GIT_TERMINAL_PROMPT": "0",
            "GIT_CONFIG_NOSYSTEM": "1",
            **os.environ,
            **(env or {}),
        }
        try:
            result = subprocess.run(
                command,
                cwd=str(workdir),
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=self.timeout,
                env=run_env,
            )
        except FileNotFoundError as exc:  # git not installed
            raise GitError("The 'git' executable is not available.") from exc
        except subprocess.TimeoutExpired as exc:
            raise GitError(f"git {' '.join(args)} timed out") from exc

        if check and result.returncode != 0:
            detail = result.stderr.strip() or result.stdout.strip()
            raise GitError(f"git {' '.join(args)} failed: {detail}")
        return result

    # -- queries -------------------------------------------------------------
    def is_repo(self) -> bool:
        return (self.path / ".git").exists()

    def has_remote(self) -> bool:
        if not self.is_repo():
            return False
        return bool(self.run("remote", check=False).stdout.strip())

    def current_branch(self) -> str:
        result = self.run("rev-parse", "--abbrev-ref", "HEAD", check=False)
        branch = result.stdout.strip()
        return "" if branch in {"", "HEAD"} else branch

    def head_sha(self) -> str:
        return self.run("rev-parse", "HEAD", check=False).stdout.strip()

    def status_porcelain(self) -> str:
        return self.run("status", "--porcelain", check=False).stdout

    def branches(self) -> list[str]:
        result = self.run("branch", "--format=%(refname:short)", check=False)
        return [line.strip() for line in result.stdout.splitlines() if line.strip()]

    def log(self, limit: int = 20) -> list[dict]:
        result = self.run(
            "log",
            f"-n{limit}",
            "--pretty=format:%H%x1f%an%x1f%ae%x1f%s%x1f%cI",
            check=False,
        )
        commits = []
        for line in result.stdout.splitlines():
            if not line.strip():
                continue
            sha, author, email, message, committed = (line.split("\x1f") + [""] * 5)[:5]
            commits.append(
                {
                    "sha": sha,
                    "author_name": author,
                    "author_email": email,
                    "message": message,
                    "committed_at": committed,
                }
            )
        return commits

    # -- mutations -----------------------------------------------------------
    def init(self, branch: str = "main") -> "GitClient":
        self.path.mkdir(parents=True, exist_ok=True)
        self.run("init", "-b", branch)
        return self

    def clone(self, url: str, branch: str | None = None) -> "GitClient":
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if self.path.exists() and any(self.path.iterdir()):
            raise GitError(f"Target directory is not empty: {self.path}")
        self.run("clone", url, str(self.path), cwd=self.path.parent)
        if branch and self.current_branch() != branch:
            self.run("checkout", branch, check=False)
        return self

    def fetch(self) -> None:
        self.run("fetch", "--all", "--prune", "--tags")

    def pull_rebase(self, branch: str) -> None:
        self.run("pull", "--rebase", "--autostash", "origin", branch)

    def add_all(self) -> None:
        self.run("add", "-A")

    def commit(self, message: str, author_name: str, author_email: str) -> str:
        self.run(
            "-c",
            f"user.name={author_name}",
            "-c",
            f"user.email={author_email}",
            "commit",
            "-m",
            message,
        )
        return self.head_sha()

    def commit_all(self, message: str, author_name: str, author_email: str) -> str | None:
        self.add_all()
        if not self.status_porcelain().strip():
            return None
        return self.commit(message, author_name, author_email)

    def push(self, branch: str, url: str | None = None) -> None:
        self.run("push", url or "origin", f"{branch}:{branch}")

    def checkout(self, branch: str) -> None:
        self.run("checkout", branch)

    def checkout_new(self, branch: str, start: str | None = None) -> None:
        args = ["checkout", "-b", branch]
        if start:
            args.append(start)
        self.run(*args)

    def ensure_branch(self, branch: str) -> None:
        if branch not in self.branches():
            self.checkout_new(branch)

    def diff_text(self, from_ref: str, to_ref: str | None = None) -> str:
        args = ["diff", from_ref]
        if to_ref:
            args.append(to_ref)
        return self.run(*args, check=False).stdout
