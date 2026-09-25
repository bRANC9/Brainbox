import uuid

from django.db import models


class KnowledgeChunk(models.Model):
    """A retrieval unit. Never a permission source: access is always resolved
    through the parent Resource ACL (terv.md 21)."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    document = models.ForeignKey(
        "documents.Document", on_delete=models.CASCADE, related_name="chunks"
    )
    resource = models.ForeignKey(
        "resources.Resource", on_delete=models.CASCADE, related_name="chunks"
    )
    workspace = models.ForeignKey(
        "workspaces.Workspace", on_delete=models.CASCADE, related_name="+"
    )
    project = models.ForeignKey(
        "workspaces.Project", on_delete=models.CASCADE, null=True, blank=True, related_name="+"
    )
    version = models.IntegerField(null=True, blank=True)
    chunk_index = models.IntegerField()
    content = models.TextField()
    summary = models.TextField(blank=True)
    embedding = models.JSONField(null=True, blank=True)
    token_count = models.IntegerField(default=0)
    status = models.CharField(max_length=16, blank=True)
    priority = models.IntegerField(default=0)
    checksum = models.CharField(max_length=64, blank=True)
    indexed_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "embeddings_knowledge_chunk"
        ordering = ["document", "chunk_index"]
        constraints = [
            models.UniqueConstraint(
                fields=["document", "chunk_index"], name="uniq_document_chunk_index"
            )
        ]
        indexes = [
            models.Index(fields=["workspace", "project"]),
            models.Index(fields=["resource"]),
        ]

    def __str__(self) -> str:
        return f"{self.document_id}#{self.chunk_index}"


class EmbeddingIndexState(models.Model):
    class Status(models.TextChoices):
        PENDING = "pending", "Pending"
        INDEXED = "indexed", "Indexed"
        ERROR = "error", "Error"

    document = models.OneToOneField(
        "documents.Document", on_delete=models.CASCADE, related_name="embedding_state"
    )
    provider = models.CharField(max_length=64, blank=True)
    model = models.CharField(max_length=128, blank=True)
    dimension = models.IntegerField(default=0)
    backend = models.CharField(max_length=64, blank=True)
    chunks_count = models.IntegerField(default=0)
    content_hash = models.CharField(max_length=64, blank=True)
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.PENDING)
    last_indexed_at = models.DateTimeField(null=True, blank=True)
    last_error = models.TextField(blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "embeddings_index_state"

    def __str__(self) -> str:
        return f"{self.document_id} {self.status}"
