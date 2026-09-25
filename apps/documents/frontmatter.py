"""YAML frontmatter helpers (Obsidian compatible)."""

from __future__ import annotations

import yaml


def parse_frontmatter(text: str) -> tuple[dict, str]:
    """Split ``text`` into ``(metadata, body)``.

    A frontmatter block starts with a ``---`` line on the very first line and
    ends with the next ``---`` line. Without frontmatter the metadata is empty
    and the body is the original text unchanged.
    """
    if not text:
        return {}, text
    if not text.startswith("---"):
        return {}, text

    newline_index = text.find("\n")
    if newline_index == -1:
        return {}, text

    end = text.find("\n---", newline_index)
    if end == -1:
        return {}, text

    raw_meta = text[newline_index + 1 : end]
    body = text[end + 4 :]
    body = body.lstrip("\n")

    try:
        metadata = yaml.safe_load(raw_meta) or {}
    except yaml.YAMLError:
        return {}, text

    if not isinstance(metadata, dict):
        return {}, text
    return metadata, body
