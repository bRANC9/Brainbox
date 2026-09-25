import uuid

from django.conf import settings
from django.db import models


class Group(models.Model):
    """Platform group, usable as an ACL subject at any resource level."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    name = models.CharField(max_length=150, unique=True)
    description = models.TextField(blank=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="created_groups",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    members = models.ManyToManyField(
        settings.AUTH_USER_MODEL,
        through="groups.GroupMembership",
        related_name="brainbox_groups",
        blank=True,
    )

    class Meta:
        db_table = "groups_group"
        ordering = ["name"]

    def __str__(self) -> str:
        return self.name


class GroupMembership(models.Model):
    class Role(models.TextChoices):
        MEMBER = "member", "Member"
        MANAGER = "manager", "Manager"
        OWNER = "owner", "Owner"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="group_memberships"
    )
    group = models.ForeignKey(
        Group, on_delete=models.CASCADE, related_name="memberships"
    )
    role = models.CharField(max_length=16, choices=Role.choices, default=Role.MEMBER)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "groups_membership"
        constraints = [
            models.UniqueConstraint(fields=["user", "group"], name="uniq_group_membership")
        ]

    def __str__(self) -> str:
        return f"{self.user} in {self.group} ({self.role})"
