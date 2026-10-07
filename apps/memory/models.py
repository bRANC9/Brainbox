"""Consolidated memory: facts extracted from documents, projected per viewer.

A fact is one assertion with a source. Its visibility is exactly the source's,
so a fact can never be seen by someone who cannot read where it came from. The
"memory" a user sees is the facts whose source is in their readable set, grouped
by subject and cited back to the document - the same person, role or process
gathered from several documents into one place, but never across the ACL.
"""

from __future__ import annotations

import uuid

from django.db import models


class Fact(models.Model):
    """One extracted assertion, always carrying its source.

    ``subject_key`` is the normalised subject used for grouping; ``subject``
    keeps the original spelling to display. ``source_resource`` is the visibility
    anchor: the fact is exactly as visible as the document it came from.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    subject = models.CharField(max_length=255)
    subject_key = models.CharField(max_length=255, db_index=True)
    predicate = models.CharField(max_length=64)
    object = models.CharField(max_length=512, blank=True)
    assertion = models.TextField(blank=True)
    source_resource = models.ForeignKey(
        "resources.Resource", on_delete=models.CASCADE, related_name="facts"
    )
    source_document = models.ForeignKey(
        "documents.Document",
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="facts",
    )
    workspace = models.ForeignKey(
        "workspaces.Workspace",
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="facts",
    )
    project = models.ForeignKey(
        "workspaces.Project",
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="facts",
    )
    confidence = models.FloatField(default=1.0)
    origin = models.CharField(
        max_length=16,
        default="deterministic",
        help_text="How the fact was found: deterministic, llm or reflection.",
    )
    dedupe_key = models.CharField(max_length=64, unique=True)
    extracted_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "memory_fact"
        ordering = ["subject_key", "predicate"]
        indexes = [models.Index(fields=["subject_key", "predicate"])]

    def __str__(self) -> str:
        return f"{self.subject} {self.predicate} {self.object}"


class MemoryAggregate(models.Model):
    """A consolidated statement about a subject, citing several sources.

    Its visibility is the **intersection** of its sources': the viewer must be
    able to read *every* source it cites, so combining a fact from one place with
    a fact from another can never reveal more than either alone.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    subject = models.CharField(max_length=255)
    subject_key = models.CharField(max_length=255, db_index=True)
    text = models.TextField()
    workspace = models.ForeignKey(
        "workspaces.Workspace",
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="memory_aggregates",
    )
    sources = models.ManyToManyField(
        "resources.Resource", related_name="memory_aggregates"
    )
    proof_count = models.PositiveIntegerField(default=0)
    confidence = models.FloatField(default=0.7)
    origin = models.CharField(max_length=16, default="deterministic")
    dedupe_key = models.CharField(max_length=64, unique=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "memory_aggregate"
        ordering = ["subject_key"]

    def __str__(self) -> str:
        return f"{self.subject}: {self.text[:40]}"
