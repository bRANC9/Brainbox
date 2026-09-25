import uuid

from django.conf import settings
from django.db import models


class LinkType(models.TextChoices):
    WIKILINK = "wikilink", "Obsidian wikilink"
    REFERENCE = "reference", "Reference"
    RELATED = "related", "Related"
    PARENT = "parent", "Parent"
    CHILD = "child", "Child"
    EMBED = "embed", "Embed"


class ResourceLink(models.Model):
    """Directed link between two Resources. Cross-workspace links are allowed.

    The link may exist even when the viewer cannot see the target content;
    traversal is always permission-checked.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    source = models.ForeignKey(
        "resources.Resource", on_delete=models.CASCADE, related_name="outgoing_links"
    )
    target = models.ForeignKey(
        "resources.Resource", on_delete=models.CASCADE, related_name="incoming_links"
    )
    link_type = models.CharField(
        max_length=16, choices=LinkType.choices, default=LinkType.REFERENCE
    )
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="created_links",
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "links_resource_link"
        ordering = ["-created_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["source", "target", "link_type"], name="uniq_resource_link"
            )
        ]
        indexes = [
            models.Index(fields=["source", "link_type"]),
            models.Index(fields=["target", "link_type"]),
        ]

    def __str__(self) -> str:
        return f"{self.source_id} -[{self.link_type}]-> {self.target_id}"
