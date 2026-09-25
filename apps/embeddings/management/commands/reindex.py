"""Reindex documents into the vector store (chunk + embed + store)."""

from django.core.management.base import BaseCommand

from apps.embeddings.services import IndexingService


class Command(BaseCommand):
    help = "Reindex all documents (chunk, embed, store)."

    def add_arguments(self, parser):
        parser.add_argument("--document-id", type=int, default=None)
        parser.add_argument("--no-force", action="store_true")

    def handle(self, *args, **options):
        if options.get("document_id"):
            from apps.documents.models import Document

            document = Document.objects.get(pk=options["document_id"])
            IndexingService.index_document(document, force=not options["no_force"])
            self.stdout.write(self.style.SUCCESS(f"Reindexed document {document.pk}."))
            return
        count = IndexingService.reindex_all(force=not options["no_force"])
        self.stdout.write(self.style.SUCCESS(f"Reindexed {count} documents."))
