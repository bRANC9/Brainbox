"""Backfill ``Workspace.owner`` / ``Project.owner`` from ``created_by``.

The owner field was added after the fact, so pre-existing rows can carry
``owner IS NULL``. That is not a cosmetic gap: with a NULL owner
``PermissionService.can_manage_acl`` answers False for *everybody*, including
the person who created the workspace, and the only remaining way in is an
audited superuser takeover.

``owner = created_by`` restores exactly the access the creator already had -
``WorkspaceService.create`` / ``ProjectService.create`` already issued them the
ADMIN grant, so this only makes the field point at the entry that was there
all along. The ACL is not touched.

Rows with no creator at all cannot be fixed here; they are reported as leftovers
because they need a human decision, not a guess.
"""

from __future__ import annotations

from django.core.management.base import BaseCommand
from django.db import IntegrityError, transaction
from django.db.models import F

from apps.workspaces.models import Project, Workspace


class Command(BaseCommand):
    help = (
        "A tulajdon nélküli (owner IS NULL) workspace-ek és projektek "
        "tulajdonosát a létrehozójára állítja. Idempotens."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Csak megmutatja, mit módosítana; nem ír adatbázist.",
        )

    def handle(self, *args, **options):
        dry_run = options["dry_run"]
        changed = 0
        failed: list[tuple[str, str]] = []

        for model, label in ((Workspace, "workspace"), (Project, "project")):
            candidates = model.objects.filter(
                owner__isnull=True, created_by__isnull=False
            ).order_by("name")
            for obj in candidates:
                name = self._label(obj)
                if dry_run:
                    self.stdout.write(f"  [száraz futás] {label}: {name} → owner={obj.created_by.username}")
                    changed += 1
                    continue
                try:
                    # Row by row, not one bulk UPDATE: uniq_personal_per_owner can
                    # reject a statement as a whole when two ownerless personal
                    # workspaces want the same owner, and a per-row failure is
                    # reportable instead of losing every other row with it.
                    with transaction.atomic():
                        model.objects.filter(pk=obj.pk).update(owner=F("created_by"))
                except IntegrityError as exc:
                    failed.append((name, f"integritási hiba: {exc}"))
                    continue
                changed += 1
                self.stdout.write(
                    self.style.SUCCESS(f"  {label}: {name} → owner={obj.created_by.username}")
                )

        leftover = self._leftovers()
        if dry_run:
            self.stdout.write(
                self.style.SUCCESS(f"[száraz futás] összesen {changed} sor módosítandó.")
            )
        else:
            self.stdout.write(self.style.SUCCESS(f"Összesen {changed} sor beállítva."))

        if failed:
            self.stdout.write(
                self.style.WARNING(f"Nem sikerült ({len(failed)}):")
            )
            for name, reason in failed:
                self.stdout.write(self.style.ERROR(f"  - {name}: {reason}"))
            self.stdout.write(
                "  Kézi beavatkozás kell: két Personális workspace ugyanahhoz a "
                "felhasználóhoz tartozik, amit a uniq_personal_per_owner "
                "megenged. Az egyiket töröld vagy alakítsd shareddé."
            )

        if leftover:
            self.stdout.write(
                self.style.WARNING(f"Maradt tulajdon nélküli sor ({len(leftover)}), created_by is NULL:")
            )
            for label, name in leftover:
                self.stdout.write(self.style.ERROR(f"  - {label}: {name}"))
            self.stdout.write(
                "  Ezekhez nincs mire visszaállítani a tulajdont. Auditált "
                "superuser-átvétel (OwnershipService.take_over) vagy a sor "
                "kézi törlése kell."
            )
        else:
            self.stdout.write("Nincs maradó tulajdon nélküli sor.")
        return None

    # -- helpers -------------------------------------------------------------
    @staticmethod
    def _label(obj) -> str:
        if isinstance(obj, Project):
            return f"{obj.workspace.name}/{obj.name}"
        return f"{obj.name} (slug={obj.slug})"

    @staticmethod
    def _leftovers() -> list[tuple[str, str]]:
        rows: list[tuple[str, str]] = []
        for workspace in Workspace.objects.filter(
            owner__isnull=True, created_by__isnull=True
        ).order_by("name"):
            rows.append(("workspace", f"{workspace.name} (slug={workspace.slug})"))
        for project in Project.objects.filter(
            owner__isnull=True, created_by__isnull=True
        ).select_related("workspace").order_by("name"):
            rows.append(("project", f"{project.workspace.name}/{project.name}"))
        return rows
