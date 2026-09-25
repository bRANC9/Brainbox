"""Markdown-aware chunking (Phase 3, terv.md 21)."""

from __future__ import annotations

import re

from django.conf import settings

from apps.documents.frontmatter import parse_frontmatter

BLOCK_SPLIT = re.compile(r"\n\s*\n")


class ChunkingService:
    @staticmethod
    def chunk(text: str, *, max_chars: int | None = None, overlap: int | None = None) -> list[str]:
        max_chars = max_chars or settings.BRAINBOX_CHUNK_SIZE
        overlap = settings.BRAINBOX_CHUNK_OVERLAP if overlap is None else overlap
        max_chars = max(max_chars, 100)
        overlap = max(0, min(overlap, max_chars // 2))

        _frontmatter, body = parse_frontmatter(text or "")
        body = (body or "").strip()
        if not body:
            return []

        chunks: list[str] = []
        current = ""
        for block in BLOCK_SPLIT.split(body):
            block = block.strip()
            if not block:
                continue
            if len(block) > max_chars:
                if current:
                    chunks.append(current)
                    current = ""
                step = max_chars - overlap
                for start in range(0, len(block), step):
                    piece = block[start : start + max_chars].strip()
                    if piece:
                        chunks.append(piece)
                continue
            if not current:
                current = block
            elif len(current) + len(block) + 2 <= max_chars:
                current = f"{current}\n\n{block}"
            else:
                chunks.append(current)
                tail = current[-overlap:].strip() if overlap else ""
                current = f"{tail}\n\n{block}".strip() if tail else block
        if current:
            chunks.append(current)
        return [chunk for chunk in chunks if chunk.strip()]
