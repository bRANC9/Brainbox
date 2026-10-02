"""Provision the per-user Personal workspace for everybody who is missing one.

``PersonalWorkspaceService`` creates it lazily - on first OIDC login, on session
login, or when the workspace list renders - but an instance can accumulate
accounts that never triggered a login after the rollout (invited by mail, created
by a migration, an API-only user). Those people have no home workspace and no way
to notice it: every screen simply looks empty.

This walks the active users and fills the gap through the same service, so the
workspace is created exactly the way an interactive login would create it
(owner's ADMIN grant, personal kind, unique per user). Idempotent: an existing
Personal workspace is never touched.
"""

from __future__ import annotations

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand

from apps.workspaces.models import Workspace, WorkspaceKind
from apps.workspaces.personal import PersonalWorkspaceService


class Command(BaseCommand):
    help = (
        "Létrehozza a hiányzó Personális workspace-t minden aktív felhasználónak. "
        "Idempotens."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Csak felsorolja, kinek hiányzik; nem hoz létre semmit.",
        )

    def handle(self, *args, **options):
        pending = self._pending()
        if not pending:
            self.stdout.write(
                self.style.SUCCESS("Minden aktív felhasználónak van Personális workspace-e.")
            )
            return

        for user in pending:
            self.stdout.write(
                f"  {user.username}"
                + (f" ({user.display_name})" if user.display_name else "")
                + f" → {PersonalWorkspaceService.slug_for(user)}"
            )

        if options["dry_run"]:
            self.stdout.write(
                self.style.WARNING(
                    f"[száraz futás] {len(pending)} Personális workspace hiányzik; nem hoztam létre semmit."
                )
            )
            return

        # Hand the service the exact list we printed, so the created count and
        # the report cannot disagree.
        created = PersonalWorkspaceService.provision_all(pending)
        self.stdout.write(
            self.style.SUCCESS(f"{created} Personális workspace létrehozva.")
        )
        for user in pending:
            workspace = PersonalWorkspaceService.get_for(user)
            if workspace is not None:
                self.stdout.write(f"  - {user.username}: {workspace.name} ({workspace.slug})")

    # -- helpers -------------------------------------------------------------
    @staticmethod
    def _pending() -> list:
        """Active users with no Personal workspace yet.

        ``get_for`` is the service's own predicate, but a single query beats one
        per user; the unique constraint on (kind=personal, owner) is what makes
        this correct rather than merely fast.
        """
        owners = set(
            Workspace.objects.filter(kind=WorkspaceKind.PERSONAL, owner__isnull=False)
            .values_list("owner_id", flat=True)
        )
        return [
            user
            for user in get_user_model()
            .objects.filter(is_active=True)
            .order_by("username")
            if user.id not in owners
        ]
