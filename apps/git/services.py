"""Git integration: attach repositories, sync, import (Obsidian) and export.

Git-backed Workspace/Project uses the repository checkout as its source of
truth (see apps.resources.storage). Pulling imports Git changes into Documents
and Files; platform writes are committed back (direct commit or feature branch).
"""

from __future__ import annotations

import hashlib
import logging
import os
import re
import shutil
from pathlib import Path

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone
from django.utils.text import slugify

from apps.audit.models import AuditAction, AuditSource
from apps.audit.services import AuditService
from apps.documents.frontmatter import parse_frontmatter
from apps.documents.models import ChangeSource, ChangeType, Document
from apps.documents.services import DocumentService
from apps.files.models import File
from apps.files.services import FileService
from apps.links.services import LinkService
from apps.resources.models import ResourceType
from apps.resources.services import ResourceService

from .git_cli import GitClient, GitError, authenticated_url
from .models import (
    GitCommitReference,
    GitRepository,
    GitSyncState,
    GitSyncStatus,
    GitWorkflow,
)

logger = logging.getLogger("brainbox.git")

DOCUMENT_SUFFIXES = {".md", ".markdown"}
IGNORED_DIRS = {".git", ".obsidian", ".trash", "node_modules", ".venv", "__pycache__"}

WIKILINK_RE = re.compile(r"!?\[\[([^\]]+)\]\]")
MDLINK_RE = re.compile(r"\[[^\]]*\]\(([^)]+)\)")


class GitService:
    # -- attachment ----------------------------------------------------------
    @classmethod
    @transaction.atomic
    def attach_repository(
        cls,
        *,
        workspace,
        project=None,
        remote_url: str = "",
        name: str = "",
        default_branch: str = "main",
        workflow: str = GitWorkflow.DIRECT_COMMIT,
        created_by=None,
        request=None,
        import_now: bool = True,
    ) -> GitRepository:
        if project is not None and project.workspace_id != workspace.pk:
            raise ValidationError({"project": "Project does not belong to the workspace."})

        display_name = name or remote_url.rsplit("/", 1)[-1] or "Repository"
        parent = project.resource if project is not None else workspace.resource
        resource = ResourceService.create(
            resource_type=ResourceType.GIT_REPOSITORY,
            name=display_name,
            created_by=created_by,
            parent=parent,
        )
        repository = GitRepository.objects.create(
            resource=resource,
            workspace=workspace,
            project=project,
            name=display_name,
            remote_url=remote_url,
            default_branch=default_branch,
            workflow=workflow,
            created_by=created_by,
        )
        GitSyncState.objects.create(repository=repository)

        client = cls._client(repository)
        try:
            if remote_url:
                client.clone(
                    authenticated_url(remote_url, settings.BRAINBOX_GIT_TOKEN),
                    branch=default_branch,
                )
            else:
                client.init(default_branch)
            cls.update_sync_state(repository)
            if import_now and remote_url:
                cls.scan_repository(repository, user=created_by, request=request)
        except GitError as exc:
            cls.set_error(repository, str(exc))
            logger.warning("git attach failed for %s: %s", repository.pk, exc)

        AuditService.log(
            AuditAction.CREATE,
            user=created_by,
            resource=resource,
            workspace=workspace,
            project=project,
            source=AuditSource.API if request is not None else AuditSource.SYSTEM,
            request=request,
            detail={"type": "git_repository", "remote_url": remote_url},
        )
        # Re-fetch so the response exposes a fresh sync_state (the reverse
        # one-to-one cache would otherwise hold the pre-clone snapshot).
        return GitRepository.objects.select_related(
            "resource", "workspace", "project", "sync_state"
        ).get(pk=repository.pk)

    @classmethod
    @transaction.atomic
    def detach_repository(cls, repository: GitRepository, *, user=None, request=None) -> None:
        directory = repository.directory
        resource = repository.resource
        AuditService.log(
            AuditAction.DELETE,
            user=user,
            resource=resource,
            workspace=repository.workspace,
            project=repository.project,
            source=AuditSource.API if request is not None else AuditSource.SYSTEM,
            request=request,
            detail={"type": "git_repository", "remote_url": repository.remote_url},
        )
        resource.delete()
        shutil.rmtree(directory, ignore_errors=True)

    @classmethod
    def repository_for(cls, *, workspace, project=None) -> GitRepository | None:
        if project is not None:
            repository = GitRepository.objects.filter(project=project, is_active=True).first()
            if repository is not None:
                return repository
        return GitRepository.objects.filter(
            workspace=workspace, project__isnull=True, is_active=True
        ).first()

    # -- remote operations ---------------------------------------------------
    @classmethod
    def pull_repository(cls, repository: GitRepository, *, user=None, request=None) -> dict:
        client = cls._client(repository)
        if not client.is_repo():
            raise GitError("Repository is not initialised.")
        if not client.has_remote():
            raise GitError("Repository has no remote to pull from.")

        try:
            client.fetch()
            client.pull_rebase(repository.default_branch)
            result = cls.scan_repository(repository, user=user, request=request)
            head = client.head_sha()
            if head:
                GitCommitReference.objects.get_or_create(
                    repository=repository,
                    sha=head,
                    direction=GitCommitReference.Direction.IMPORT,
                    defaults={
                        "branch": repository.default_branch,
                        "message": "Imported from Git",
                        "author_name": settings.BRAINBOX_GIT_AUTHOR_NAME,
                        "author_email": settings.BRAINBOX_GIT_AUTHOR_EMAIL,
                    },
                )
            cls.update_sync_state(repository, pulled=True)
        except GitError as exc:
            cls.set_error(repository, str(exc))
            raise

        AuditService.log(
            AuditAction.GIT_PULL,
            user=user,
            resource=repository.resource,
            workspace=repository.workspace,
            project=repository.project,
            source=AuditSource.API if request is not None else AuditSource.SYSTEM,
            request=request,
            detail={"remote_url": repository.remote_url, **result},
        )
        return result

    @classmethod
    def commit_repository(
        cls, *, repository: GitRepository, message: str, user=None, request=None
    ) -> str | None:
        client = cls._client(repository)
        if not client.is_repo():
            return None

        author_name, author_email = cls._author(user)
        branch = client.current_branch() or repository.default_branch
        if repository.workflow == GitWorkflow.BRANCH_PR:
            feature = cls._feature_branch(user)
            if client.current_branch() != feature:
                client.ensure_branch(feature)
            branch = feature

        sha = client.commit_all(message, author_name, author_email)
        if sha is None:
            return None

        pushed = False
        if repository.remote_url:
            try:
                url = authenticated_url(repository.remote_url, settings.BRAINBOX_GIT_TOKEN)
                client.push(branch, url=url)
                pushed = True
            except GitError as exc:
                # A failed push must not break the platform write that triggered it.
                cls.set_error(repository, str(exc))
                logger.warning("git push failed for %s: %s", repository.pk, exc)

        GitCommitReference.objects.create(
            repository=repository,
            sha=sha,
            branch=branch,
            message=message,
            author_name=author_name,
            author_email=author_email,
            direction=GitCommitReference.Direction.EXPORT,
            created_by=user,
        )
        cls.update_sync_state(repository, pushed=pushed)
        AuditService.log(
            AuditAction.GIT_COMMIT,
            user=user,
            resource=repository.resource,
            workspace=repository.workspace,
            project=repository.project,
            source=AuditSource.API if request is not None else AuditSource.SYSTEM,
            request=request,
            git_commit=sha,
            detail={"branch": branch, "pushed": pushed, "message": message},
        )
        return sha

    @classmethod
    def push_repository(cls, repository: GitRepository, *, user=None, request=None) -> bool:
        client = cls._client(repository)
        if not repository.remote_url:
            raise GitError("Repository has no remote to push to.")
        branch = client.current_branch() or repository.default_branch
        client.push(branch, url=authenticated_url(repository.remote_url, settings.BRAINBOX_GIT_TOKEN))
        cls.update_sync_state(repository, pushed=True)
        AuditService.log(
            AuditAction.GIT_PUSH,
            user=user,
            resource=repository.resource,
            workspace=repository.workspace,
            project=repository.project,
            source=AuditSource.API if request is not None else AuditSource.SYSTEM,
            request=request,
            detail={"branch": branch},
        )
        return True

    @classmethod
    def status_repository(cls, repository: GitRepository) -> dict:
        client = cls._client(repository)
        state = getattr(repository, "sync_state", None)
        if not client.is_repo():
            return {"initialised": False, "status": state.status if state else "idle"}
        return {
            "initialised": True,
            "branch": client.current_branch(),
            "head": client.head_sha(),
            "dirty": bool(client.status_porcelain().strip()),
            "remote_url": repository.remote_url,
            "branches": client.branches(),
            "commits": client.log(limit=10),
            "status": state.status if state else GitSyncStatus.IDLE,
            "last_error": state.last_error if state else "",
        }

    @classmethod
    def diff_repository(cls, repository: GitRepository, from_ref: str, to_ref: str = "") -> str:
        return cls._client(repository).diff_text(from_ref, to_ref or None)

    # -- import / scan -------------------------------------------------------
    @classmethod
    @transaction.atomic
    def scan_repository(cls, repository: GitRepository, *, user=None, request=None) -> dict:
        root = repository.directory
        if not root.exists():
            raise GitError("Repository directory does not exist.")

        workspace = repository.workspace
        project = repository.project
        documents = {
            document.path: document
            for document in Document.objects.filter(workspace=workspace, project=project)
        }
        files = {
            stored.path: stored
            for stored in File.objects.filter(workspace=workspace, project=project)
        }

        counts = {
            "documents_created": 0,
            "documents_updated": 0,
            "files_created": 0,
            "files_updated": 0,
            "links": 0,
        }

        for path in cls._walk(root):
            rel_path = path.relative_to(root).as_posix()
            if path.suffix.lower() in DOCUMENT_SUFFIXES:
                content = path.read_text(encoding="utf-8", errors="replace")
                title = cls._document_title(content, rel_path)
                existing = documents.get(rel_path)
                if existing is None:
                    documents[rel_path] = DocumentService.create(
                        workspace=workspace,
                        project=project,
                        title=title,
                        path=rel_path,
                        content=content,
                        created_by=user,
                        source=ChangeSource.GIT,
                        request=request,
                    )
                    counts["documents_created"] += 1
                else:
                    latest = existing.versions.order_by("-version").first()
                    if latest is None or latest.content != content:
                        DocumentService.update_content(
                            document=existing,
                            content=content,
                            title=title,
                            user=user,
                            source=ChangeSource.GIT,
                            change_type=ChangeType.IMPORT,
                            request=request,
                        )
                        counts["documents_updated"] += 1
            else:
                data = path.read_bytes()
                existing = files.get(rel_path)
                if existing is None:
                    FileService.create(
                        workspace=workspace,
                        project=project,
                        name=path.name,
                        data=data,
                        path=rel_path,
                        created_by=user,
                        source=ChangeSource.GIT,
                        request=request,
                    )
                    counts["files_created"] += 1
                elif existing.checksum != cls._checksum(data):
                    FileService.update_content(
                        stored_file=existing,
                        data=data,
                        user=user,
                        source=ChangeSource.GIT,
                        request=request,
                    )
                    counts["files_updated"] += 1

        counts["links"] = cls.rebuild_links(workspace=workspace, project=project, user=user)
        cls.update_sync_state(repository)
        return counts

    @classmethod
    def rebuild_links(cls, *, workspace, project=None, user=None) -> int:
        """Rebuild outgoing links for every document in the repository scope."""
        return LinkService.rebuild_all(
            workspace=workspace, project=project, user=user
        )

    # -- auto commit (platform -> Git) --------------------------------------
    @classmethod
    def autocommit_document(cls, *, document, user=None, request=None, message=None) -> str | None:
        repository = cls.repository_for(workspace=document.workspace, project=document.project)
        if repository is None or not repository.auto_sync:
            return None
        return cls.commit_repository(
            repository=repository,
            message=message or f"Update document: {document.path}",
            user=user,
            request=request,
        )

    @classmethod
    def autocommit_file(cls, *, stored_file, user=None, request=None, message=None) -> str | None:
        repository = cls.repository_for(workspace=stored_file.workspace, project=stored_file.project)
        if repository is None or not repository.auto_sync:
            return None
        return cls.commit_repository(
            repository=repository,
            message=message or f"Update file: {stored_file.path}",
            user=user,
            request=request,
        )

    # -- sync state helpers --------------------------------------------------
    @classmethod
    def update_sync_state(
        cls, repository: GitRepository, *, pulled: bool = False, pushed: bool = False
    ) -> GitSyncState:
        state, _ = GitSyncState.objects.get_or_create(repository=repository)
        client = cls._client(repository)
        if client.is_repo():
            state.branch = client.current_branch() or repository.default_branch
            state.last_synced_sha = client.head_sha()
            state.status = GitSyncStatus.IDLE
            state.last_error = ""
        if pulled:
            state.last_pulled_at = timezone.now()
        if pushed:
            state.last_pushed_at = timezone.now()
        state.save()
        return state

    @classmethod
    def set_error(cls, repository: GitRepository, message: str) -> GitSyncState:
        state, _ = GitSyncState.objects.get_or_create(repository=repository)
        state.status = GitSyncStatus.ERROR
        state.last_error = message[:2000]
        state.save(update_fields=["status", "last_error", "updated_at"])
        return state

    # -- internals -----------------------------------------------------------
    @classmethod
    def _client(cls, repository: GitRepository) -> GitClient:
        return GitClient(repository.directory, timeout=settings.BRAINBOX_GIT_COMMAND_TIMEOUT)

    @staticmethod
    def _author(user) -> tuple[str, str]:
        if user is not None and getattr(user, "is_authenticated", False):
            name = user.display_name or user.get_full_name() or user.username
            email = user.email or settings.BRAINBOX_GIT_AUTHOR_EMAIL
            return name, email
        return settings.BRAINBOX_GIT_AUTHOR_NAME, settings.BRAINBOX_GIT_AUTHOR_EMAIL

    @staticmethod
    def _feature_branch(user) -> str:
        who = "auto"
        if user is not None and getattr(user, "is_authenticated", False):
            who = slugify(user.username) or "auto"
        return f"brainbox/{who}"

    @staticmethod
    def _checksum(data: bytes) -> str:
        return hashlib.sha256(data).hexdigest()

    @staticmethod
    def _document_title(content: str, rel_path: str) -> str:
        frontmatter, _body = parse_frontmatter(content)
        title = str(frontmatter.get("title") or "").strip()
        return title or Path(rel_path).stem

    @staticmethod
    def _walk(root: Path):
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = [
                name
                for name in dirnames
                if name not in IGNORED_DIRS and not name.startswith(".")
            ]
            for filename in filenames:
                if filename.startswith("."):
                    continue
                yield Path(dirpath) / filename


__all__ = [
    "GitService",
    "GitRepository",
    "GitSyncState",
    "GitCommitReference",
    "GitWorkflow",
    "GitSyncStatus",
    "GitError",
]
