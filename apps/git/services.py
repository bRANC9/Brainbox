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
    GitCredential,
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
        secret=None,
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
            secret=secret,
            default_branch=default_branch,
            workflow=workflow,
            created_by=created_by,
        )
        GitSyncState.objects.create(repository=repository)

        client = cls._client(repository)
        try:
            if remote_url:
                # Clone with the credential, then store the CLEAN remote so no
                # token is persisted in .git/config on disk.
                token, style, username = cls._auth_spec(repository)
                client.clone(
                    authenticated_url(remote_url, token, style=style, username=username),
                    branch=default_branch,
                    reset_url=remote_url,
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
            auth_url = cls._auth_url(repository)
            client.fetch(url=auth_url)
            client.pull_rebase(repository.default_branch, url=auth_url)
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

        # Author = who wrote the change; credential = whose PAT pushed it.
        author_name, author_email = cls._identity(user)
        push_url, credential_owner = cls._resolve_credential(repository, user)

        branch = client.current_branch() or repository.default_branch
        if repository.workflow == GitWorkflow.BRANCH_PR:
            feature = cls._feature_branch(user)
            if client.current_branch() != feature:
                client.ensure_branch(feature)
            branch = feature

        full_message = message
        if credential_owner and credential_owner != f"{author_name} <{author_email}>":
            # Shared credential was used: name its owner so the history stays
            # traceable (git/GitHub render Co-authored-by natively).
            full_message = f"{message}\n\nCo-authored-by: {credential_owner}"

        sha = client.commit_all(full_message, author_name, author_email)
        if sha is None:
            return None

        pushed = False
        if repository.remote_url:
            try:
                client.push(branch, url=push_url)
                pushed = True
            except GitError as exc:
                # A failed push must not break the platform write that triggered it.
                cls.set_error(repository, str(exc))
                logger.warning("git push failed for %s: %s", repository.pk, exc)

        GitCommitReference.objects.create(
            repository=repository,
            sha=sha,
            branch=branch,
            message=full_message,
            author_name=author_name,
            author_email=author_email,
            direction=GitCommitReference.Direction.EXPORT,
            created_by=user if getattr(user, "is_authenticated", False) else None,
        )
        cls.update_sync_state(repository, pushed=pushed)
        AuditService.log(
            AuditAction.GIT_COMMIT,
            user=user,
            source=AuditSource.API if request is not None else AuditSource.SYSTEM,
            request=request,
            git_commit=sha,
            detail={
                "branch": branch,
                "pushed": pushed,
                "message": message,
                "author": f"{author_name} <{author_email}>",
                "credential_owner": credential_owner,
            },
        )
        return sha

    @classmethod
    def push_repository(cls, repository: GitRepository, *, user=None, request=None) -> bool:
        client = cls._client(repository)
        if not repository.remote_url:
            raise GitError("Repository has no remote to push to.")
        branch = client.current_branch() or repository.default_branch
        client.push(branch, url=cls._auth_url(repository))
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

    @classmethod
    def _auth_token(cls, repository: GitRepository) -> str:
        """Credential for this repository: its Vault secret, else the global token.

        A per-repository secret must be owned by the user who created the
        repository (that user delegated the credential to the resource) and must
        be attached to the repository's workspace/project. The reveal is audited
        and the value is only ever passed to git on the command line.
        """
        if not repository.secret_id:
            return settings.BRAINBOX_GIT_TOKEN or ""

        from django.core.exceptions import PermissionDenied

        from apps.secrets.services import SecretService

        secret = repository.secret
        owner = repository.created_by or secret.owner
        if secret.owner_id != getattr(owner, "id", None):
            raise GitError(
                "The repository credential is not owned by the user who created the repository."
            )
        try:
            return SecretService.reveal(
                secret,
                user=secret.owner,
                workspace_id=str(repository.workspace_id),
                project_id=str(repository.project_id) if repository.project_id else None,
            )
        except PermissionDenied as exc:
            raise GitError(
                f"Credential '{secret.name}' is not usable here ({exc}). "
                "Attach it to the repository's workspace/project."
            ) from exc

    @classmethod
    def _auth_spec(cls, repository: GitRepository) -> tuple[str, str, str]:
        """Return ``(token, style, username)`` for this repository.

        The style can be pinned in the secret's metadata, otherwise it is derived
        from the remote host (Azure DevOps -> basic auth, GitHub -> x-access-token).
        """
        token = cls._auth_token(repository)
        style = "auto"
        username = ""
        metadata = getattr(repository.secret, "metadata", None) if repository.secret_id else None
        if metadata:
            style = str(metadata.get("auth_style") or "auto")
            username = str(metadata.get("username") or "")
        return token, style, username

    @classmethod
    def _auth_url(cls, repository: GitRepository) -> str:
        token, style, username = cls._auth_spec(repository)
        return authenticated_url(repository.remote_url, token, style=style, username=username)

    # -- per-user credentials + authorship -----------------------------------
    @classmethod
    def _user_credential(cls, repository: GitRepository, user):
        """The acting user's own credential for this repository, if any."""
        if user is None or not getattr(user, "is_authenticated", False):
            return None
        credential = (
            GitCredential.objects.select_related("secret", "secret__owner")
            .filter(repository=repository, user=user, secret__is_active=True)
            .first()
        )
        return credential

    @classmethod
    def _resolve_credential(cls, repository: GitRepository, user):
        """Resolve the credential to push with.

        Returns ``(url, owner_label)`` where ``owner_label`` identifies whose
        credential was used (``None`` = no auth needed, e.g. a public repo).
        """
        own = cls._user_credential(repository, user)
        if own is not None:
            value = cls._reveal_for(own.secret, user, repository)
            style, username = cls._style_of(own.secret)
            return (
                authenticated_url(
                    repository.remote_url, value, style=style, username=username
                ),
                None,  # their own credential -> no co-author needed
            )

        if repository.secret_id:
            token, style, username = cls._auth_spec(repository)
            return (
                authenticated_url(
                    repository.remote_url, token, style=style, username=username
                ),
                cls._label_for(repository.secret.owner),
            )

        token = settings.BRAINBOX_GIT_TOKEN or ""
        if not token:
            return (repository.remote_url, None)
        return (
            authenticated_url(repository.remote_url, token, style="auto"),
            cls._label_for(repository.created_by) if repository.created_by else None,
        )

    @staticmethod
    def _label_for(user) -> str | None:
        if user is None or not getattr(user, "is_authenticated", False):
            return None
        return GitService._identity(user)[1]

    @staticmethod
    def _style_of(secret) -> tuple[str, str]:
        metadata = getattr(secret, "metadata", None) or {}
        return (
            str(metadata.get("auth_style") or "auto"),
            str(metadata.get("username") or ""),
        )

    @staticmethod
    def _identity(user) -> tuple[str, str]:
        """Git author name/email for a user (email is mandatory for git)."""
        if user is None or not getattr(user, "is_authenticated", False):
            return settings.BRAINBOX_GIT_AUTHOR_NAME, settings.BRAINBOX_GIT_AUTHOR_EMAIL
        name = (
            user.display_name
            or user.get_full_name()
            or user.username
            or settings.BRAINBOX_GIT_AUTHOR_NAME
        )
        email = user.email or f"{user.username}@brainbox.local"
        return name, email

    @staticmethod
    def _reveal_for(secret, user, repository: GitRepository) -> str:
        from django.core.exceptions import PermissionDenied

        from apps.secrets.services import SecretService

        try:
            return SecretService.reveal(
                secret,
                user=user if getattr(user, "is_authenticated", False) else secret.owner,
                workspace_id=str(repository.workspace_id),
                project_id=str(repository.project_id) if repository.project_id else None,
            )
        except PermissionDenied as exc:
            raise GitError(
                f"Credential '{secret.name}' is not usable here ({exc})."
            ) from exc

    @classmethod
    def set_user_credential(cls, *, repository: GitRepository, user, secret):
        """Register/refresh a user's own credential for a repository."""
        credential, _created = GitCredential.objects.update_or_create(
            repository=repository, user=user, defaults={"secret": secret}
        )
        return credential

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
