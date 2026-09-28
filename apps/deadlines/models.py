import uuid
from datetime import date

from django.db import models


class DeadlineStatus(models.TextChoices):
    OPEN = "open", "Open"
    DONE = "done", "Done"
    SNOOZED = "snoozed", "Snoozed"
    DISMISSED = "dismissed", "Dismissed"


class DeadlineSource(models.TextChoices):
    FRONTMATTER = "frontmatter", "Frontmatter"
    INLINE = "inline", "Inline text"
    MANUAL = "manual", "Manual"


class KnowledgeDeadline(models.Model):
    """A date extracted from (or added alongside) a knowledge document.

    Detection is derived from the file - the file stays the source of truth.
    Editing a document re-extracts its auto-detected deadlines; manually added
    ones are preserved.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    document = models.ForeignKey(
        "documents.Document", on_delete=models.CASCADE, related_name="deadlines"
    )
    resource = models.ForeignKey(
        "resources.Resource", on_delete=models.CASCADE, related_name="deadlines"
    )
    workspace = models.ForeignKey(
        "workspaces.Workspace", on_delete=models.CASCADE, related_name="deadlines"
    )
    project = models.ForeignKey(
        "workspaces.Project",
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="deadlines",
    )
    title = models.CharField(max_length=300)
    due_date = models.DateField(db_index=True)
    status = models.CharField(
        max_length=16, choices=DeadlineStatus.choices, default=DeadlineStatus.OPEN, db_index=True
    )
    source = models.CharField(
        max_length=16, choices=DeadlineSource.choices, default=DeadlineSource.INLINE
    )
    confidence = models.IntegerField(default=50)
    context = models.TextField(blank=True)
    source_ref = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "deadlines_knowledge_deadline"
        ordering = ["due_date", "title"]
        constraints = [
            models.UniqueConstraint(
                fields=["document", "due_date", "title", "source"],
                name="uniq_deadline_per_document",
            )
        ]
        indexes = [
            models.Index(fields=["status", "due_date"]),
            models.Index(fields=["workspace", "due_date"]),
        ]

    def __str__(self) -> str:
        return f"{self.due_date}: {self.title[:60]}"

    @property
    def is_overdue(self) -> bool:
        return self.status == DeadlineStatus.OPEN and self.due_date < date.today()
