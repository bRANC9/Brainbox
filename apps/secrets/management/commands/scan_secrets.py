"""Scan knowledge documents for committed credentials."""

from django.core.management.base import BaseCommand

from apps.documents.models import Document
from apps.documents.services import DocumentService
from apps.secrets.scanner import scan_document_content


class Command(BaseCommand):
    help = "Scan documents for likely secrets (values are redacted)."

    def add_arguments(self, parser):
        parser.add_argument("--document-id", type=int, default=None)
        parser.add_argument("--fail-on-found", action="store_true")

    def handle(self, *args, **options):
        queryset = Document.objects.all()
        if options.get("document_id"):
            queryset = queryset.filter(pk=options["document_id"])

        total = 0
        for document in queryset.iterator():
            findings = scan_document_content(DocumentService.read_content(document))
            if not findings:
                continue
            total += len(findings)
            self.stdout.write(f"{document.path}: " + ", ".join(f["type"] for f in findings))

        if total:
            self.stdout.write(self.style.WARNING(f"{total} potential secret(s) found."))
            if options["fail_on_found"]:
                raise SystemExit(1)
        else:
            self.stdout.write(self.style.SUCCESS("No secrets detected."))
