"""The per-user Personal workspace.

Every user gets exactly one Personal workspace: a private home for their own
notes, drafts and scratch knowledge. It is provisioned automatically (on first
OIDC login, on session login, or lazily when the workspace list is rendered) and
is **never shareable** - there is one holder, and the permission engine rejects
every other ACL entry on it, at the service layer, not just in the UI.

A Personal workspace is not a subject-matter category: it is a statement about
audience. Shared workspaces are the opposite - private when created, opened up
only by an explicit grant.
"""

from __future__ import annotations

from django.db import transaction
from django.utils.text import slugify

from .models import Workspace, WorkspaceKind
from .services import WorkspaceService

PERSONAL_SLUG_PREFIX = "personal"


def _label(user) -> str:
    return (
        getattr(user, "display_name", "")
        or getattr(user, "username", "")
        or "personal"
    ).strip() or "personal"


class PersonalWorkspaceService:
    @staticmethod
    def name_for(user) -> str:
        return f"Personal – {_label(user)}"

    @staticmethod
    def slug_for(user) -> str:
        return f"{PERSONAL_SLUG_PREFIX}-{slugify(_label(user))[:40] or user.pk}"

    @staticmethod
    def get_for(user) -> Workspace | None:
        if user is None or not getattr(user, "is_authenticated", False):
            return None
        return (
            Workspace.objects.filter(kind=WorkspaceKind.PERSONAL, owner=user)
            .select_related("resource")
            .first()
        )

    @classmethod
    @transaction.atomic
    def get_or_create(cls, user) -> Workspace:
        """Idempotent: at most one Personal workspace per user, ever."""
        existing = cls.get_for(user)
        if existing is not None:
            return existing
        return WorkspaceService.create(
            name=cls.name_for(user),
            slug=cls.slug_for(user),
            description="Személyes jegyzetfüzet. Csak Te látod, megosztani nem lehet.",
            created_by=user,
            owner=user,
            kind=WorkspaceKind.PERSONAL,
        )

    @staticmethod
    def provision_all(queryset=None) -> int:
        """Backfill for every active user. Returns how many were created."""
        from django.contrib.auth import get_user_model

        users = queryset if queryset is not None else get_user_model().objects.filter(
            is_active=True
        )
        created = 0
        for user in users:
            if PersonalWorkspaceService.get_for(user) is None:
                PersonalWorkspaceService.get_or_create(user)
                created += 1
        return created


__all__ = ["PersonalWorkspaceService", "WorkspaceKind"]
