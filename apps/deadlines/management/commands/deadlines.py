"""Re-extract deadlines from knowledge files."""

from django.core.management.base import BaseCommand

from apps.deadlines.models import DeadlineSource, KnowledgeDeadline
from apps.deadlines.services import rebuild_deadlines
from apps.documents.models import Document


class Command(BaseCommand):
    help = "Extract deadlines from documents (frontmatter + inline text)."

    def add_arguments(self, parser):
        parser.add_argument("--document-id", type=int, default=None)
        parser.add_argument("--reindex", action="store_true", help="Rescan all documents.")
        parser.add_argument(
            "--prune", action="store_true", help="Delete auto deadlines with no source left."
        )

    def handle(self, *args, **options):
        queryset = Document.objects.select_related("workspace", "project", "resource")
        if options["document_id"]:
            queryset = queryset.filter(pk=options["document_id"])

        total = 0
        for document in queryset.iterator():
            total += rebuild_deadlines(document)

        if options["prune"]:
            stale = KnowledgeDeadline.objects.filter(
                source__in=[DeadlineSource.FRONTMATTER, DeadlineSource.INLINE]
            ).delete()
            self.stdout.write(f"pruned {stale[0]} stale deadline rows")

        self.stdout.write(self.style.SUCCESS(f"{total} deadline(s) extracted"))
