"""Delete an empty workspace, or refuse loudly.

``manage.py bootstrap`` creates a single shared "Personal" workspace on first
boot. That was the right shape before per-user Personal workspaces existed; now
it is an orphan holding one workspace-level ADMIN grant, and every new user gets
a workspace of their own next to it. This command removes it - but only when it
is genuinely empty, because a cascade is not a decision.

The deletion goes through the workspace's Resource: projects, folders,
documents, files, git repositories, deadlines and chunks all hang off the
Resource tree, so one delete cleans up the whole subtree consistently. The guard
is what makes that safe.

Two refusals, deliberately different:

* Documents always block, ``--force`` or not. A document is a file on disk with a
  version history and derived rows; dropping it through a cascade would leave the
  bytes and the search index behind. That is a ``DocumentService.delete`` call,
  one document at a time, on purpose.
* Everything else (projects, files, folders, other resource types) can be
  overridden with ``--force``, but only after the command has printed exactly
  what is in there.

Confirmation is mandatory: without ``--yes`` the command prints the plan and
exits non-zero.
"""

from __future__ import annotations

from django.core.management.base import BaseCommand, CommandError

from apps.documents.models import Document, DocumentFolder
from apps.files.models import File
from apps.resources.models import Resource, ResourceType
from apps.workspaces.models import Project, Workspace, WorkspaceKind

#: Bound on the resource-tree walk, so a corrupted parent cycle cannot spin.
MAX_WALK_DEPTH = 16

#: Inventory buckets, in the words the operator reads. Hungarian does not
#: inflect a noun after a number, so one bucket per noun is enough.
PROJECTS = "projekt"
DOCUMENTS = "dokumentum"
FILES = "fájl"
FOLDERS = "mappa"
OTHER = "egyéb erőforrás"


class Command(BaseCommand):
    help = (
        "Töröl egy workspace-t slug alapján, de csak ha teljesen üres. "
        "Dokumentumot sosem töröl, akkor sem, ha --force."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--slug",
            required=True,
            help="A törlendő workspace slugje.",
        )
        parser.add_argument(
            "--force",
            action="store_true",
            help=(
                "Engedi a törlést akkor is, ha projekt, mappa, fájl vagy más "
                "erőforrás van benne. Dokumentumnál továbbra sem segít."
            ),
        )
        parser.add_argument(
            "--yes",
            action="store_true",
            help="Megerősíti a törlést. Enélkül csak a tervet írja ki és nem nulla a kilépéssel tér vissza.",
        )

    def handle(self, *args, **options):
        slug = options["slug"]
        workspace = Workspace.objects.filter(slug=slug).select_related("resource").first()
        if workspace is None:
            raise CommandError(f"Nincs ilyen workspace: '{slug}'.")

        counts = self._inventory(workspace)
        documents = counts[DOCUMENTS]
        other = {key: value for key, value in counts.items() if key != DOCUMENTS and value}

        if documents:
            raise self._fail(
                f"'{slug}' {documents} dokumentumot tartalmaz, ezért NEM törlöm. "
                "A dokumentum fájl a lemezen, verziótörténettel és indexelt "
                "szöveggel; ezt a DocumentService.delete-del, dokumentumonként "
                "kell megtenni, szándékosan. Ha tényleg mindent el kell dobni, "
                "előbb a dokumentumokat, utána ezt a parancsot.",
                counts=counts,
            )

        if workspace.kind == WorkspaceKind.PERSONAL:
            self.stdout.write(
                self.style.WARNING(
                    f"Figyelem: '{slug}' Personális workspace. Biztos vagyok benne, "
                    "hogy ez az első boot által létrehozott közös hagyaték, nem valakié?"
                )
            )

        if other:
            listing = ", ".join(f"{value} {key}" for key, value in sorted(other.items()))
            if not options["force"]:
                raise self._fail(
                    f"'{slug}' nem üres: {listing}. Ha ezt tényleg le akarod törölni, "
                    "add meg a --force flaget.",
                    counts=counts,
                )
            self.stdout.write(
                self.style.WARNING(f"--force: '{slug}' tartalma ({listing}) cascadedolva törlődik.")
            )

        if not options["yes"]:
            self.stdout.write(
                f"Terv: törölném a(z) '{workspace.name}' workspace-t "
                f"(slug={workspace.slug}, resource={workspace.resource_id}). "
                f"Dokumentum: {documents}."
            )
            for key, value in sorted(counts.items()):
                self.stdout.write(f"  - {value} {key}")
            raise self._fail(
                "Nincs megerősítve. Ha biztos vagy benne, add meg a --yes-t."
            )

        # The Resource is the identity: deleting it cascades the whole subtree and
        # the Workspace row itself, instead of leaving orphaned children behind.
        workspace.resource.delete()
        self.stdout.write(
            self.style.SUCCESS(f"A(z) '{workspace.name}' workspace törölve ({workspace.resource_id}).")
        )
        return None

    # -- helpers -------------------------------------------------------------
    def _fail(self, message: str, *, counts=None) -> CommandError:
        """Build a CommandError after printing the inventory to stdout.

        CommandError is printed on stderr, which is block-buffered when the output
        is piped; without this the operator would read the refusal first and the
        inventory second, i.e. backwards. Listing the non-zero buckets only keeps
        the refusal short when the reason is a single document.
        """
        if counts is not None:
            for key, value in sorted(counts.items()):
                if value:
                    self.stdout.write(f"  - {value} {key}")
        self.stdout.flush()
        return CommandError(message)

    @staticmethod
    def _inventory(workspace: Workspace) -> dict:
        """What is in the workspace, keyed by the Hungarian noun the operator reads.

        The four named buckets are the ones the guards are written against. The
        last bucket catches everything else hanging off the Resource tree, which
        would otherwise disappear silently in the cascade - a git repository, for
        instance, whose checkout on disk is not part of the database.
        """
        root = workspace.resource
        counts = {
            PROJECTS: Project.objects.filter(workspace=workspace).count(),
            DOCUMENTS: Document.objects.filter(workspace=workspace).count(),
            FILES: File.objects.filter(workspace=workspace).count(),
            FOLDERS: DocumentFolder.objects.filter(workspace=workspace).count(),
        }
        counts[OTHER] = sum(
            1
            for resource in Command._descendants(root)
            if resource.resource_type
            not in (
                ResourceType.PROJECT,
                ResourceType.DOCUMENT,
                ResourceType.FILE,
                ResourceType.FOLDER,
            )
        )
        return counts

    @staticmethod
    def _descendants(root: Resource) -> list[Resource]:
        seen: dict[object, Resource] = {}
        current = [root.id]
        for _ in range(MAX_WALK_DEPTH):
            children = list(Resource.objects.filter(parent_id__in=current).exclude(id__in=seen))
            if not children:
                break
            for child in children:
                seen[child.id] = child
            current = [child.id for child in children]
        return list(seen.values())
