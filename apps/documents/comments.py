"""Comments on documents.

A comment inherits the document's visibility exactly. Reading the document is
enough to read and add comments (a reader may discuss what they read); resolving
or deleting needs to be the author or to hold write on the document - a reader
cannot edit someone else's note. Every action is audited against the document's
Resource.
"""

from __future__ import annotations

from django.core.exceptions import PermissionDenied, ValidationError

from apps.audit.models import AuditAction, AuditSource
from apps.audit.services import AuditService
from apps.permissions.constants import Permission
from apps.permissions.services import PermissionService

from .models import DocumentComment


class CommentService:
    @staticmethod
    def _require(user, resource, permission, *, api_key=None) -> None:
        if not PermissionService.check(user, resource, permission, api_key=api_key):
            raise PermissionDenied("Nincs jogosultságod ehhez a dokumentumhoz.")

    @classmethod
    def list_for(cls, document, user, *, api_key=None) -> list[DocumentComment]:
        cls._require(user, document.resource, Permission.READ, api_key=api_key)
        return list(document.comments.select_related("author"))

    @classmethod
    def add(cls, *, document, author, body, request=None, api_key=None) -> DocumentComment:
        body = (body or "").strip()
        if not body:
            raise ValidationError({"body": "Üres hozzászólás."})
        cls._require(author, document.resource, Permission.READ, api_key=api_key)
        comment = DocumentComment.objects.create(
            document=document, author=author, body=body
        )
        AuditService.log(
            AuditAction.CREATE,
            user=author,
            api_key=api_key,
            resource=document.resource,
            source=AuditSource.API if request is not None else AuditSource.SYSTEM,
            request=request,
            detail={"type": "comment", "comment_id": str(comment.pk)},
        )
        return comment

    @classmethod
    def set_resolved(
        cls, *, comment, user, resolved, request=None, api_key=None
    ) -> DocumentComment:
        cls._may_edit(comment, user, api_key=api_key)
        comment.resolved = resolved
        comment.save(update_fields=["resolved", "updated_at"])
        AuditService.log(
            AuditAction.UPDATE,
            user=user,
            api_key=api_key,
            resource=comment.document.resource,
            source=AuditSource.API if request is not None else AuditSource.SYSTEM,
            request=request,
            detail={"type": "comment", "comment_id": str(comment.pk), "resolved": resolved},
        )
        return comment

    @classmethod
    def delete(cls, *, comment, user, request=None, api_key=None) -> None:
        cls._may_edit(comment, user, api_key=api_key)
        resource = comment.document.resource
        comment_id = str(comment.pk)
        comment.delete()
        AuditService.log(
            AuditAction.DELETE,
            user=user,
            api_key=api_key,
            resource=resource,
            source=AuditSource.API if request is not None else AuditSource.SYSTEM,
            request=request,
            detail={"type": "comment", "comment_id": comment_id},
        )

    @classmethod
    def _may_edit(cls, comment, user, *, api_key=None) -> None:
        if user is not None and comment.author_id == getattr(user, "id", None):
            return
        if not PermissionService.check(
            user, comment.document.resource, Permission.WRITE, api_key=api_key
        ):
            raise PermissionDenied("Nincs jogosultságod ehhez a hozzászóláshoz.")
