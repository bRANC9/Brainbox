"""Indexing pipeline: chunk -> embed -> store (Phase 3)."""

from __future__ import annotations

import hashlib
import logging

from django.utils import timezone

from apps.documents.services import DocumentService

from .chunking import ChunkingService
from .models import EmbeddingIndexState, KnowledgeChunk
from .providers import get_embedding_provider
from .vectorstores import get_vector_store

logger = logging.getLogger("brainbox.embeddings")


class IndexingService:
    @classmethod
    def index_document(cls, document, *, force: bool = False) -> EmbeddingIndexState:
        content = DocumentService.read_content(document)
        content_hash = hashlib.sha256((content or "").encode("utf-8")).hexdigest()

        state, _ = EmbeddingIndexState.objects.get_or_create(document=document)
        if (
            not force
            and state.status == EmbeddingIndexState.Status.INDEXED
            and state.content_hash == content_hash
        ):
            return state

        provider = get_embedding_provider()
        store = get_vector_store()

        pieces = ChunkingService.chunk(content)
        vectors = provider.embed(pieces) if pieces else []

        KnowledgeChunk.objects.filter(document=document).delete()
        store.delete_document(document.pk)

        chunks = []
        for index, piece in enumerate(pieces):
            embedding = vectors[index] if index < len(vectors) else None
            chunks.append(
                KnowledgeChunk(
                    document=document,
                    resource=document.resource,
                    workspace=document.workspace,
                    project=document.project,
                    version=document.current_version,
                    chunk_index=index,
                    content=piece,
                    summary=(document.summary or "")[:500],
                    embedding=embedding,
                    token_count=len(piece.split()),
                    status=document.status,
                    priority=document.priority,
                    checksum=hashlib.sha256(piece.encode("utf-8")).hexdigest(),
                )
            )
        KnowledgeChunk.objects.bulk_create(chunks)
        try:
            store.upsert(chunks)
        except Exception as exc:  # external store failure must not lose local chunks
            logger.warning("vector store upsert failed: %s", exc)

        state.provider = provider.name
        state.model = getattr(provider, "model", "")
        state.dimension = provider.dimension
        state.backend = store.name
        state.chunks_count = len(chunks)
        state.content_hash = content_hash
        state.status = EmbeddingIndexState.Status.INDEXED
        state.last_error = ""
        state.last_indexed_at = timezone.now()
        state.save()
        return state

    @classmethod
    def remove_document(cls, document) -> None:
        KnowledgeChunk.objects.filter(document=document).delete()
        EmbeddingIndexState.objects.filter(document=document).delete()
        try:
            get_vector_store().delete_document(document.pk)
        except Exception as exc:  # pragma: no cover - external store
            logger.warning("vector store delete failed: %s", exc)

    @classmethod
    def reindex_all(cls, *, force: bool = True) -> int:
        from apps.documents.models import Document

        count = 0
        for document in Document.objects.select_related("workspace", "project", "resource").iterator():
            cls.index_document(document, force=force)
            count += 1
        return count
