"""First-class tags for anything in the tree.

Tags attach to documents, folders, files, projects and workspaces through a
generic relation, so the vocabulary stays uniform. A tag on a folder applies to
everything underneath it when filtering (and is shown on children), which makes
"tag everything in this folder" a one-liner.
"""


from django.conf import settings
from django.contrib.contenttypes.fields import GenericForeignKey, GenericRelation
from django.contrib.contenttypes.models import ContentType
from django.db import models


class Tag(models.Model):
    name = models.CharField(max_length=64, unique=True)
    color = models.CharField(max_length=16, blank=True, default="")
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="created_tags",
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "tags_tag"
        ordering = ["name"]

    def __str__(self) -> str:
        return self.name


class TaggedItem(models.Model):
    tag = models.ForeignKey(Tag, on_delete=models.CASCADE, related_name="items")
    content_type = models.ForeignKey(ContentType, on_delete=models.CASCADE)
    object_id = models.UUIDField(db_index=True)
    target = GenericForeignKey("content_type", "object_id")
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="tag_assignments",
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "tags_tagged_item"
        ordering = ["tag__name"]
        constraints = [
            models.UniqueConstraint(
                fields=["tag", "content_type", "object_id"], name="uniq_tagged_item"
            )
        ]
        indexes = [models.Index(fields=["content_type", "object_id"])]

    def __str__(self) -> str:
        return f"{self.tag.name} -> {self.content_type.model}:{self.object_id}"


class Taggable(models.Model):
    """Mixin giving any model tag support."""

    tagged_items = GenericRelation(TaggedItem)

    class Meta:
        abstract = True
