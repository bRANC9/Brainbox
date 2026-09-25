from django.contrib import admin

from .models import EmbeddingIndexState, KnowledgeChunk


@admin.register(EmbeddingIndexState)
class EmbeddingIndexStateAdmin(admin.ModelAdmin):
    list_display = ("document", "status", "provider", "backend", "chunks_count", "last_indexed_at")
    list_filter = ("status", "provider", "backend")
    search_fields = ("document__title",)
    readonly_fields = [field.name for field in EmbeddingIndexState._meta.fields]


@admin.register(KnowledgeChunk)
class KnowledgeChunkAdmin(admin.ModelAdmin):
    list_display = ("document", "chunk_index", "token_count", "status", "priority", "indexed_at")
    list_filter = ("status",)
    search_fields = ("document__title", "content")
    readonly_fields = ("id", "indexed_at")
