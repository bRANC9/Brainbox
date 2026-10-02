"""Seed a throwaway instance for UI screenshots.

Mirrors the shape of the real instance so the screenshots show something
recognisable: Ecoform with nested projects and a deep folder tree, the renamed
BrainBox workspace with the plan docs, a per-user Personal workspace, and two
other users with different access - because "is it legible" and "does it leak"
are both questions about what the UI actually shows.
"""

from __future__ import annotations

from apps.accounts.models import User
from apps.documents.services import DocumentService
from apps.groups.models import Group, GroupMembership
from apps.permissions.constants import Permission, SubjectType
from apps.permissions.services import PermissionService
from apps.workspaces.services import ProjectService, WorkspaceService

NESTED = {
    "azure-functions": [
        ("Bevezetes.md", "# Bevezetes\n\nAz első lépések a függvényekkel.\n"),
        ("trigger.md", "# Trigger\n\n## Időzített trigger\n\n* 1 percenként\n* manuális\n"),
        ("monitoring/alert-rules.md", "# Alert szabályok\n\nCPU > 80% esetén riaszt.\n"),
        ("monitoring/dashboards/prod.json.md", "# Dashboard\n\nTermelési nézet.\n"),
    ],
    "terraform": [
        ("README.md", "# Terraform\n\nA `main.tf` az entrypoint.\n"),
        ("modules/vnet.md", "# VNet modul\n\nCsupasz hálózat a hubhoz.\n"),
        ("modules/vnet/peering.md", "# Peering\n\nA hub VNet peering.\n"),
        ("state/backend.md", "# State backend\n\n**NE** a repóban.\n"),
    ],
}


def run():
    branc = User.objects.create_user("kerek.kristof", "kerek.kristof@ecoform.hu", "pw")
    branc.display_name = "Kristóf"
    branc.is_staff = True
    branc.is_superuser = True
    branc.save()

    dev = User.objects.create_user("dev.one", "dev.one@ecoform.hu", "pw")
    dev.display_name = "Dev One"
    lead = User.objects.create_user("lead.two", "lead.two@ecoform.hu", "pw")
    lead.display_name = "Lead Two"
    outsider = User.objects.create_user("outsider", "outsider@example.com", "pw")
    outsider.display_name = "Külső"

    devs = Group.objects.create(name="Fejlesztők")
    leads = Group.objects.create(name="Vezetők")
    GroupMembership.objects.create(user=dev, group=devs, role="member")
    GroupMembership.objects.create(user=lead, group=leads, role="manager")
    GroupMembership.objects.create(user=dev, group=leads)

    ecoform = WorkspaceService.create(
        name="Ecoform",
        description="Céges tudástár: projektek, deploy, döntések.",
        created_by=branc,
    )
    for group, perm in ((devs, Permission.WRITE), (leads, Permission.ADMIN)):
        PermissionService.grant(
            ecoform.resource,
            subject_type=SubjectType.GROUP,
            subject_id=group.id,
            permission=perm,
            created_by=branc,
        )

    deploy = ProjectService.create(
        workspace=ecoform, name="Deploy", description="Telepítési doksik.", created_by=branc
    )
    dev_area = ProjectService.create(
        workspace=ecoform, name="Fejlesztői rész", created_by=branc
    )
    for folder, docs in NESTED.items():
        for path, content in docs:
            DocumentService.create(
                workspace=ecoform,
                project=dev_area,
                title=path.rsplit("/", 1)[-1].removesuffix(".md"),
                path=f"{folder}/{path}",
                content=content,
                summary=content.splitlines()[0].lstrip("# "),
                created_by=branc,
            )
    DocumentService.create(
        workspace=ecoform,
        project=deploy,
        title="AI Handler — deploy",
        path="ai-handler.md",
        content="# AI Handler\n\n## Deploy\n\nA `main.bicep` kanonikus.\n",
        summary="A pipeline what-if kötelező.",
        created_by=branc,
    )

    brainbox = WorkspaceService.create(
        name="BrainBox",
        description="Fejlesztői jegyzetek: tervek, döntések, állapot.",
        created_by=branc,
        slug="brainbox",
    )
    bb = ProjectService.create(
        workspace=brainbox,
        name="Brainbox — jogosultságok",
        description="ACL modell újratervezése.",
        created_by=branc,
    )
    for title, path in (
        ("Brainbox — jogosultsági modell újratervezése (terv)", "terv.md"),
        ("Projekt-állapot", "allapot.md"),
    ):
        DocumentService.create(
            workspace=brainbox,
            project=bb,
            title=title,
            path=path,
            content=f"# {title}\n\n## 1. Döntés\n\nA superuser bypass kivéve.\n",
            summary="A döntett modell.",
            created_by=branc,
        )

    # A deep folder granted to `dev`, to make the trail-node rendering visible.
    runbooks = DocumentService.create(
        workspace=ecoform,
        project=dev_area,
        title="Éles runbook",
        path="runbooks/2024/runbook.md",
        content="# Éles runbook\n\nLépés 1: mérd.\n",
        created_by=branc,
    )
    from apps.documents.folders import ensure_folder

    folder = ensure_folder(ecoform, dev_area, "runbooks/2024", created_by=branc)
    PermissionService.grant(
        folder.resource,
        subject_type=SubjectType.USER,
        subject_id=outsider.id,
        permission=Permission.READ,
        created_by=branc,
    )
    runbooks.resource.parent = folder.resource
    runbooks.resource.save(update_fields=["parent"])

    print("seeded")
    print("  superuser : kerek.kristof / pw")
    print("  developer : dev.one / pw        (Fejlesztők: write a teljes Ecoform-on)")
    print("  lead      : lead.two / pw       (Vezetők: admin)")
    print("  outsider  : outsider / pw")
    print("  owned     : runbooks/2024 -> dev.one")
    print("  untitled  : runbook.md in there (should stay invisible to outsider)")


run()