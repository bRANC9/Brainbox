import uuid

from django.conf import settings
from django.db import models

from .constants import Effect, Permission, SubjectType


class ResourceACL(models.Model):
    """One ACL entry on one Resource for one subject (user / group / api key).

    Relative order in the inheritance chain and the ``inherit`` flag are
    resolved by :class:`apps.permissions.services.PermissionService`.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    resource = models.ForeignKey(
        "resources.Resource", on_delete=models.CASCADE, related_name="acl_entries"
    )
    subject_type = models.CharField(max_length=16, choices=SubjectType.choices)
    subject_id = models.UUIDField()
    permission = models.CharField(max_length=16, choices=Permission.choices)
    effect = models.CharField(max_length=8, choices=Effect.choices, default=Effect.ALLOW)
    inherit = models.BooleanField(default=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="created_acl_entries",
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "permissions_resource_acl"
        ordering = ["resource", "subject_type", "permission"]
        constraints = [
            models.UniqueConstraint(
                fields=["resource", "subject_type", "subject_id", "permission"],
                name="uniq_resource_acl_entry",
            )
        ]
        indexes = [
            models.Index(fields=["subject_type", "subject_id"]),
        ]

    def __str__(self) -> str:
        return f"{self.resource_id} {self.effect}:{self.permission} -> {self.subject_type}:{self.subject_id}"
