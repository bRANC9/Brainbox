"""Secret vault services (Phase 5).

Secrets are user-owned. Attachments scope *where* a secret may be used; the
value is decrypted only for the owner and the action is audited (never the
value itself).
"""

from __future__ import annotations

import hashlib
import json

from django.core.exceptions import PermissionDenied
from django.db import transaction
from django.utils import timezone

from apps.audit.models import AuditAction, AuditResult, AuditSource
from apps.audit.services import AuditService

from .crypto import get_secret_backend
from .models import Secret, SecretAttachment, SecretType


class SecretService:
    # -- helpers -------------------------------------------------------------
    @staticmethod
    def _serialize(payload) -> str:
        if isinstance(payload, str):
            return payload
        return json.dumps(payload, sort_keys=True)

    @staticmethod
    def _fingerprint(plaintext: str) -> str:
        return hashlib.sha256(plaintext.encode("utf-8")).hexdigest()

    @classmethod
    def metadata(cls, secret: Secret) -> dict:
        return {
            "id": str(secret.id),
            "name": secret.name,
            "secret_type": secret.secret_type,
            "description": secret.description,
            "is_active": secret.is_active,
            "fingerprint": secret.fingerprint,
            "metadata": secret.metadata,
            "last_used_at": secret.last_used_at.isoformat() if secret.last_used_at else None,
            "attachments": [
                {
                    "workspace": str(a.workspace_id) if a.workspace_id else None,
                    "project": str(a.project_id) if a.project_id else None,
                }
                for a in secret.attachments.all()
            ],
        }

    # -- crud ----------------------------------------------------------------
    @classmethod
    @transaction.atomic
    def create(
        cls,
        *,
        owner,
        name: str,
        payload,
        secret_type: str = SecretType.GENERIC,
        description: str = "",
        metadata: dict | None = None,
        request=None,
    ) -> Secret:
        plaintext = cls._serialize(payload)
        secret = Secret(
            owner=owner,
            name=name,
            secret_type=secret_type,
            description=description,
            encrypted_payload=get_secret_backend().encrypt(plaintext),
            fingerprint=cls._fingerprint(plaintext),
            metadata=metadata or {},
        )
        secret.full_clean(exclude=["encrypted_payload", "fingerprint"])
        secret.save()
        AuditService.log(
            AuditAction.CREATE,
            user=owner,
            source=AuditSource.API if request is not None else AuditSource.SYSTEM,
            request=request,
            detail={"type": "secret", "name": name, "secret_type": secret_type},
        )
        return secret

    @classmethod
    @transaction.atomic
    def update(
        cls,
        *,
        secret: Secret,
        payload=None,
        description: str | None = None,
        metadata: dict | None = None,
        secret_type: str | None = None,
        request=None,
    ) -> Secret:
        if payload is not None:
            plaintext = cls._serialize(payload)
            secret.encrypted_payload = get_secret_backend().encrypt(plaintext)
            secret.fingerprint = cls._fingerprint(plaintext)
        if description is not None:
            secret.description = description
        if metadata is not None:
            secret.metadata = metadata
        if secret_type:
            secret.secret_type = secret_type
        secret.save()
        AuditService.log(
            AuditAction.UPDATE,
            user=secret.owner,
            source=AuditSource.API if request is not None else AuditSource.SYSTEM,
            request=request,
            detail={"type": "secret", "name": secret.name},
        )
        return secret

    # -- attachments ---------------------------------------------------------
    @classmethod
    def attach(
        cls, *, secret: Secret, workspace=None, project=None, created_by=None
    ) -> SecretAttachment:
        attachment, _ = SecretAttachment.objects.get_or_create(
            secret=secret,
            workspace=workspace,
            project=project,
            defaults={"created_by": created_by},
        )
        return attachment

    @classmethod
    def detach(cls, *, secret: Secret, workspace=None, project=None) -> int:
        deleted, _ = SecretAttachment.objects.filter(
            secret=secret, workspace=workspace, project=project
        ).delete()
        return deleted

    # -- authorization -------------------------------------------------------
    @classmethod
    def can_use(cls, secret: Secret, user, *, workspace_id=None, project_id=None) -> bool:
        if user is None or secret.owner_id != getattr(user, "id", None):
            return False
        if not secret.is_active:
            return False
        attachments = list(secret.attachments.all())
        if not attachments:
            return True
        return any(
            attachment.matches(workspace_id, project_id) for attachment in attachments
        )

    @classmethod
    def reveal(
        cls,
        secret: Secret,
        *,
        user,
        workspace_id=None,
        project_id=None,
        request=None,
        api_key=None,
    ) -> str:
        if not cls.can_use(secret, user, workspace_id=workspace_id, project_id=project_id):
            AuditService.log(
                AuditAction.USE_SECRET,
                user=user,
                api_key=api_key,
                result=AuditResult.DENIED,
                source=AuditSource.API if request is not None else AuditSource.SYSTEM,
                request=request,
                detail={"secret_id": str(secret.pk), "name": secret.name},
            )
            raise PermissionDenied("You are not allowed to use this secret here.")

        value = get_secret_backend().decrypt(secret.encrypted_payload)
        secret.last_used_at = timezone.now()
        secret.save(update_fields=["last_used_at", "updated_at"])
        AuditService.log(
            AuditAction.USE_SECRET,
            user=user,
            api_key=api_key,
            source=AuditSource.API if request is not None else AuditSource.SYSTEM,
            request=request,
            detail={"secret_id": str(secret.pk), "name": secret.name},
        )
        return value
