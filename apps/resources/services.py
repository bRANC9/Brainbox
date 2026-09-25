"""Application services for the Resource identity layer."""

from __future__ import annotations

from .models import Resource, ResourceType


class ResourceService:
    @staticmethod
    def create(
        *,
        resource_type: str,
        name: str,
        created_by=None,
        parent: Resource | None = None,
        metadata: dict | None = None,
    ) -> Resource:
        return Resource.objects.create(
            resource_type=resource_type,
            name=name,
            created_by=created_by,
            parent=parent,
            metadata=metadata or {},
        )

    @staticmethod
    def rebuild_chain(resource: Resource) -> list[Resource]:
        """Return the ancestor chain (self first) with parents prefetched."""
        return list(resource.ancestors())


__all__ = ["ResourceService", "ResourceType"]
