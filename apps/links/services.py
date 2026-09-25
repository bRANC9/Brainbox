"""Application services for resource links."""

from __future__ import annotations

from apps.audit.models import AuditAction, AuditSource
from apps.audit.services import AuditService

from .models import LinkType, ResourceLink


class LinkService:
    @staticmethod
    def create(*, source, target, link_type: str = LinkType.REFERENCE, created_by=None,
               request=None):
        link, created = ResourceLink.objects.get_or_create(
            source=source,
            target=target,
            link_type=link_type,
            defaults={"created_by": created_by},
        )
        if created:
            AuditService.log(
                AuditAction.UPDATE,
                user=created_by,
                resource=source,
                source=AuditSource.API if request is not None else AuditSource.SYSTEM,
                request=request,
                detail={
                    "type": "resource_link",
                    "target": str(target.id),
                    "link_type": link_type,
                },
            )
        return link

    @staticmethod
    def delete(*, source, target=None, link_type: str | None = None) -> int:
        queryset = ResourceLink.objects.filter(source=source)
        if target is not None:
            queryset = queryset.filter(target=target)
        if link_type:
            queryset = queryset.filter(link_type=link_type)
        deleted, _ = queryset.delete()
        return deleted
