"""Read-only admin for the retrieval index.

A chunk is never a permission source - access is resolved through the parent
Resource ACL - so the rows are pure derived state.

Write path: :class:`~apps.embeddings.services.IndexingService`
(``manage.py reindex``, and the automatic reindex after a document change).
"""

from django.contrib import admin

from apps.resources.admin import ResourceScopedAdminMixin

from .models import EmbeddingIndexState, KnowledgeChunk


@admin.register(EmbeddingIndexState)
class EmbeddingIndexStateAdmin(ResourceScopedAdminMixin, admin.ModelAdmin):
    resource_id_fields = ("document__resource_id",)
    list_display = ("document", "status", "provider", "backend", "chunks_count", "last_indexed_at")
    list_filter = ("status", "provider", "backend")
    search_fields = ("document__title",)
    readonly_fields = [field.name for field in EmbeddingIndexState._meta.fields]


@admin.register(KnowledgeChunk)
class KnowledgeChunkAdmin(ResourceScopedAdminMixin, admin.ModelAdmin):
    resource_id_fields = ("resource_id",)
    list_display = ("document", "chunk_index", "token_count", "status", "priority", "indexed_at")
    list_filter = ("status",)
    search_fields = ("document__title", "content")
    # `embedding` is the vector itself and `checksum` the input hash: both are
    # recomputed by the indexer, never typed in.
    readonly_fields = ("id", "resource", "embedding", "checksum", "indexed_at")
