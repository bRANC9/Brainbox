"""Application services for accounts / API keys."""

from django.db import transaction

from apps.audit.models import AuditAction, AuditResult, AuditSource
from apps.audit.services import AuditService
from apps.resources.models import Resource

from .models import ApiKey, ApiKeyScope, User


class OwnerConflict(Exception):
    """A user still owns shared content, so deleting them is refused.

    Carries the blocking objects so the caller can answer 409 with something
    actionable instead of the bare ``ProtectedError`` Django would raise - which
    is a 500, and tells the operator nothing about what to do next.
    """

    def __init__(self, workspaces: list, projects: list):
        self.workspaces = workspaces
        self.projects = projects
        super().__init__("A felhasználó még megosztott tartalmat birtokol.")

    @property
    def detail(self) -> dict:
        return {
            "error": "A felhasználó még megosztott tartalmat birtokol.",
            "owned_workspaces": [
                {"id": str(ws.pk), "name": ws.name, "slug": ws.slug} for ws in self.workspaces
            ],
            "owned_projects": [
                {"id": str(pr.pk), "name": pr.name, "workspace": str(pr.workspace_id)}
                for pr in self.projects
            ],
            "hint": (
                "Adj át minden birtokolt workspace-t és projektet "
                "(Jogosultságok → Átruházás), vagy ne töröld a felhasználót, "
                "hanem deaktiváld (Eltávolítás). Deaktiválás: is_active=false."
            ),
        }


class UserService:
    """Deleting a user is the wrong operation; deactivating is the right one.

    ``Workspace.owner`` / ``Project.owner`` are ``PROTECT``, so a user who owns
    shared content cannot be deleted at all - a 300-document workspace must not
    go down with one row in the user table. This service turns that into an
    answer an operator can act on, and handles the one case where deletion *is*
    correct: a Personal workspace has exactly one holder and no meaning without
    them, so it goes with the user.
    """

    @staticmethod
    def owned_by(user) -> tuple[list, list]:
        """Shared workspaces and projects the user owns (Personal excluded)."""
        from apps.workspaces.models import Project, Workspace, WorkspaceKind

        workspaces = list(
            Workspace.objects.filter(owner=user)
            .exclude(kind=WorkspaceKind.PERSONAL)
            .order_by("name")
        )
        projects = list(Project.objects.filter(owner=user).order_by("name"))
        return workspaces, projects

    @staticmethod
    def can_delete(user) -> bool:
        workspaces, projects = UserService.owned_by(user)
        return not workspaces and not projects

    @staticmethod
    @transaction.atomic
    def delete(*, user, actor=None, request=None) -> None:
        """Delete a user, or refuse with the list of what still blocks it.

        Their Personal workspace goes with them; shared content must be handed
        over first, and the caller is told exactly which.
        """
        workspaces, projects = UserService.owned_by(user)
        if workspaces or projects:
            raise OwnerConflict(workspaces, projects)

        from apps.workspaces.personal import PersonalWorkspaceService

        personal = PersonalWorkspaceService.get_for(user)
        if personal is not None:
            # Delete the Resource, not the Workspace row: every folder/document
            # Resource hangs off it and cascades, so nothing is left orphaned.
            Resource.objects.filter(pk=personal.resource_id).delete()

        AuditService.log(
            AuditAction.DELETE,
            user=actor or user,
            result=AuditResult.SUCCESS,
            source=AuditSource.WEB if request is None else AuditSource.API,
            request=request,
            detail={
                "type": "user",
                "username": user.username,
                "personal_workspace_deleted": personal is not None,
            },
        )
        user.delete()

    @staticmethod
    @transaction.atomic
    def deactivate(*, user, actor=None, request=None) -> User:
        """Offboarding without data loss: the account stops working, rows stay.

        This is what 95% of "remove this user" actually means - especially with
        OIDC, where the identity provider is the source of truth and the local
        row is a mirror of it. It also sidesteps the ownership question entirely.
        """
        if not user.is_active and not user.is_superuser:
            return user
        user.is_active = False
        # A deactivated superuser must stop being a superuser, or the account
        # keeps full control-plane power through the API key auth path.
        user.is_superuser = False
        user.is_staff = False
        user.save(update_fields=["is_active", "is_superuser", "is_staff"])
        ApiKey.objects.filter(user=user, revoked_at__isnull=True).update(revoked_at=_now())
        AuditService.log(
            AuditAction.UPDATE,
            user=actor or user,
            source=AuditSource.WEB if request is None else AuditSource.API,
            request=request,
            detail={"type": "user_deactivated", "username": user.username},
        )
        return user


def _now():
    from django.utils import timezone

    return timezone.now()


class ApiKeyService:
    @staticmethod
    @transaction.atomic
    def create(*, user, name, scopes=None, expires_at=None, actor=None, request=None):
        """Create a key and return ``(api_key, raw_key)``.

        ``scopes`` is a list of dicts accepted by :class:`ApiKeyScope`
        (``workspace``/``project``/``permission``/``effect``).
        """
        raw_key = ApiKey.generate_raw_key()
        api_key = ApiKey(user=user, name=name, expires_at=expires_at)
        api_key.set_key(raw_key)
        api_key.full_clean(exclude=["key_prefix", "key_hash"])
        api_key.save()

        for scope in scopes or []:
            ApiKeyScope.objects.create(api_key=api_key, **scope)

        AuditService.log(
            AuditAction.CREATE_API_KEY,
            user=actor or user,
            result=AuditResult.SUCCESS,
            source=AuditSource.WEB if request is None else AuditSource.API,
            request=request,
            detail={"api_key_id": str(api_key.id), "name": api_key.name},
        )
        return api_key, raw_key


__all__ = ["ApiKeyService", "UserService", "OwnerConflict"]
