"""Rollout audit for ``BRAINBOX_SUPERUSER_BYPASS``.

A superuser has no implicit content access. The only way in is the explicit,
audited takeover, and ``PermissionService.superuser_bypass()`` is the temporary
escape hatch that restores the old "superuser sees everything" behaviour while
the ACLs catch up.

The moment that flag becomes ``0`` every superuser loses - silently, at once -
each resource they were reaching through the bypass and never through a grant.
This command computes that set *before* the switch so the operator can write the
missing grants (or take over, which is audited) instead of discovering the gap
from a support ticket.

The set is computed with the bypass force-disabled via ``override_settings``, so
the answer is the same whether the flag is currently on or off: it is always the
list of resources that only the bypass can reach.

This is a full scan: every resource is resolved once per active superuser,
through the real ACL chain. It is a rollout command, run once before the switch,
not something to put on a schedule. ``--resource-type`` narrows it when only one
kind of object is in question.

Read-only. It walks the ACL chain and never writes a row.
"""

from __future__ import annotations

import json

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand
from django.test.utils import override_settings

from apps.permissions.constants import Permission
from apps.permissions.services import PermissionService
from apps.resources.models import Resource, ResourceType
from apps.workspaces.models import Project, Workspace

#: Bucket for resources that hang off no workspace (secrets, root git repos).
NO_WORKSPACE = "(nincs munkaterület)"


class Command(BaseCommand):
    help = (
        "Kilistázza, mely erőforrásokat lát egy superuser csak a "
        "BRAINBOX_SUPERUSER_BYPASS keresztül, hogy a kapcsoló átbillentése előtt "
        "hiányzó jogokat ki lehessen adni. Csak olvas, nem ír semmit."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--json",
            action="store_true",
            help="Gépileg feldolgozható JSON a szöveges jelentés helyett.",
        )
        parser.add_argument(
            "--resource-type",
            default="",
            choices=list(ResourceType.values),
            metavar="TYPE",
            help=(
                "Csak ezt az erőforrástípust vizsgálja (alap: mindet). "
                f"Lehetséges: {', '.join(ResourceType.values)}."
            ),
        )

    def handle(self, *args, **options):
        resources = self._resources(options["resource_type"])
        superusers = list(
            get_user_model()
            .objects.filter(is_active=True, is_superuser=True)
            .order_by("username")
        )
        report = self._build_report(superusers, resources)
        if options["json"]:
            self.stdout.write(json.dumps(report, indent=2, ensure_ascii=False, default=str))
            return
        self._render(report)

    # -- collection ----------------------------------------------------------
    @staticmethod
    def _resources(resource_type: str) -> list[Resource]:
        queryset = Resource.objects.all()
        if resource_type:
            queryset = queryset.filter(resource_type=resource_type)
        return list(queryset)

    def _build_report(self, superusers: list, resources: list[Resource]) -> dict:
        report = {
            "bypass_enabled": PermissionService.superuser_bypass(),
            "scanned_resource_count": len(resources),
            "total_superusers": len(superusers),
            "total_at_risk": 0,
            "superusers": [],
            "ownerless": self._ownerless(),
            "takeover_locked": self._takeover_locked(resources),
        }
        for user in superusers:
            at_risk = self._at_risk(user, resources)
            report["total_at_risk"] += len(at_risk)
            report["superusers"].append(
                {
                    "id": str(user.id),
                    "username": user.username,
                    "display_name": user.display_name,
                    "at_risk_count": len(at_risk),
                    "workspaces": self._group_by_workspace(at_risk),
                }
            )
        return report

    def _at_risk(self, user, resources: list[Resource]) -> list[Resource]:
        """Resources ``user`` may not read once the bypass is off.

        The decision itself is delegated to :class:`PermissionService` with the
        flag forced off, so this command can never drift from the real
        resolution rules - it only chooses *what to ask about*.
        """
        at_risk: list[Resource] = []
        with override_settings(BRAINBOX_SUPERUSER_BYPASS=False):
            for resource in resources:
                if not PermissionService.check(user, resource, Permission.READ):
                    at_risk.append(resource)
        return at_risk

    @staticmethod
    def _group_by_workspace(resources: list[Resource]) -> list[dict]:
        buckets: dict[str, dict] = {}
        for resource in resources:
            workspace_resource = resource.workspace_resource()
            if workspace_resource is None:
                key = NO_WORKSPACE
                label, slug = NO_WORKSPACE, None
            else:
                workspace = getattr(workspace_resource, "workspace", None)
                key = str(workspace_resource.id)
                label = workspace.name if workspace else workspace_resource.name
                slug = workspace.slug if workspace else None
            bucket = buckets.setdefault(key, {"workspace": label, "slug": slug, "resources": []})
            bucket["resources"].append(
                {
                    "id": str(resource.id),
                    "type": resource.resource_type,
                    "name": resource.name,
                }
            )
        return sorted(buckets.values(), key=lambda item: item["workspace"].lower())

    @staticmethod
    def _ownerless() -> list[dict]:
        """Workspaces/projects with ``owner IS NULL``.

        Nobody owns them, so :meth:`PermissionService.can_manage_acl` answers
        False for everyone and the only way in is an audited superuser takeover.
        """
        rows: list[dict] = []
        for workspace in Workspace.objects.filter(owner__isnull=True).order_by("name"):
            rows.append(
                {
                    "kind": "workspace",
                    "workspace": workspace.name,
                    "name": workspace.name,
                    "slug": workspace.slug,
                    "workspace_kind": workspace.kind,
                    "created_by": workspace.created_by.username if workspace.created_by_id else None,
                }
            )
        for project in Project.objects.filter(owner__isnull=True).select_related("workspace").order_by(
            "name"
        ):
            rows.append(
                {
                    "kind": "project",
                    "workspace": project.workspace.name,
                    "name": f"{project.workspace.name}/{project.name}",
                    "slug": project.slug,
                    "workspace_kind": None,
                    "created_by": project.created_by.username if project.created_by_id else None,
                }
            )
        return rows

    @staticmethod
    def _takeover_locked(resources: list[Resource]) -> list[dict]:
        """Resources where ``no_takeover`` closes the last recovery path.

        The flag inherits, so a locked workspace locks its whole subtree. Only
        the roots are reported, with the number of resources each one buries:
        a subtree is the operator's problem, a root is the owner's decision.
        """
        roots: dict[str, dict] = {}
        for resource in resources:
            if not resource.takeover_locked():
                continue
            lock = next((node for node in resource.ancestors() if node.no_takeover), None)
            key = str(lock.id) if lock is not None else str(resource.id)
            entry = roots.setdefault(
                key,
                {
                    "id": key,
                    "type": lock.resource_type if lock is not None else resource.resource_type,
                    "name": lock.name if lock is not None else resource.name,
                    "locked_here": lock is not None,
                    "locked_resource_count": 0,
                },
            )
            entry["locked_resource_count"] += 1
        return sorted(roots.values(), key=lambda item: (item["type"], item["name"].lower()))

    # -- rendering -----------------------------------------------------------
    def _render(self, report: dict) -> None:
        bypass = report["bypass_enabled"]
        self.stdout.write("Superuser-keresztülzetes audit (BRAINBOX_SUPERUSER_BYPASS)")
        self.stdout.write(
            f"  kapcsoló jelenleg: {int(bypass)} "
            f"({'bypass aktív' if bypass else 'bypass kikapcsolva'})"
        )
        self.stdout.write(
            f"  vizsgált erőforrás: {report['scanned_resource_count']}, "
            f"aktív superuser: {report['total_superusers']}"
        )
        if not bypass:
            self.stdout.write(
                self.style.WARNING(
                    "  A bypass már kikapcsolva: ezek a superuserek mostantól nem látják "
                    "az alábbi erőforrásokat."
                )
            )
        self.stdout.write("")

        if not report["superusers"]:
            self.stdout.write("Nincs aktív superuser, nincs mit auditálni.")
            self.stdout.write("")
        for entry in report["superusers"]:
            label = entry["username"]
            if entry["display_name"]:
                label = f"{entry['username']} ({entry['display_name']})"
            self.stdout.write(self.style.WARNING(f"superuser: {label} <{entry['id']}>"))
            if not entry["at_risk_count"]:
                self.stdout.write("  nincs kockázatos erőforrás: mindenre van ACL.")
                self.stdout.write("")
                continue
            self.stdout.write(f"  {entry['at_risk_count']} erőforrás, amit a bypass nélkül nem látna:")
            for bucket in entry["workspaces"]:
                suffix = f" (slug={bucket['slug']})" if bucket["slug"] else ""
                self.stdout.write(f"    [{bucket['workspace']}{suffix}]")
                for resource in bucket["resources"]:
                    self.stdout.write(f"      - {resource['type']}: {resource['name']}")
            self.stdout.write("")

        self._render_ownerless(report["ownerless"])
        self._render_takeover_locked(report["takeover_locked"])

        self.stdout.write(
            self.style.WARNING(
                f"Összesen {report['total_superusers']} superuser, "
                f"{report['total_at_risk']} kockázatos erőforrás."
            )
        )
        self.stdout.write(
            "A kapcsoló: BRAINBOX_SUPERUSER_BYPASS=0. Előtte adj explicit jogot "
            "(PermissionService.grant) vagy végezz auditált átvételt "
            "(OwnershipService.take_over) minden felsorolt erőforrásra."
        )

    def _render_ownerless(self, rows: list[dict]) -> None:
        if not rows:
            self.stdout.write("Tulajdon nélküli (owner IS NULL) workspace/projekt: nincs.")
            return
        self.stdout.write(
            self.style.WARNING(
                f"Tulajdon nélküli (owner IS NULL) workspace/projekt: {len(rows)}"
            )
        )
        for row in rows:
            creator = row["created_by"] or "nincs létrehozó"
            self.stdout.write(f"  - {row['kind']}: {row['name']} (created_by={creator})")

    def _render_takeover_locked(self, rows: list[dict]) -> None:
        self.stdout.write("")
        if not rows:
            self.stdout.write("no_takeover erőforrás: nincs.")
            return
        locked = sum(row["locked_resource_count"] for row in rows)
        self.stdout.write(
            self.style.WARNING(
                f"no_takeover: {len(rows)} gyökér, összesen {locked} erőforrás a tiltott fában."
            )
        )
        for row in rows:
            where = "itt van bekapcsolva" if row["locked_here"] else "öröklődik innen"
            self.stdout.write(
                f"  - {row['type']}: {row['name']} ({where}; "
                f"{row['locked_resource_count']} erőforrás érintett)"
            )
        self.stdout.write(
            "  Ezekhez nincs superuser-átvétel: csak a workspace tulajdonosa oldhatja fel."
        )
