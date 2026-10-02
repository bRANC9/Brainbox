import uuid

from django.conf import settings
from django.db import models


class Group(models.Model):
    """Platform group, usable as an ACL subject at any resource level.

    Distinct from ``django.contrib.auth.models.Group`` (which only carries
    per-model admin permissions). ACLs in Brainbox use *this* group.
    """

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
        verbose_name = "knowledge group"
        verbose_name_plural = "knowledge groups"

    def __str__(self) -> str:
        return self.name


class GroupMembership(models.Model):
    """Membership in a knowledge group.

    ``MANAGER`` administers the group: it can add and remove members. There is
    no separate owner role - the managers *are* the group's owners, which is why
    a group needs no owner field of its own.

    The role is deliberately **not** part of the permission engine: holding
    ``MANAGER`` grants no access to any resource. It only governs who may change
    the group's membership. (Adding yourself to a group that has access *is* an
    escalation, so group administration is not available to ``is_staff``.)
    """

    class Role(models.TextChoices):
        MEMBER = "member", "Member"
        MANAGER = "manager", "Manager"

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
        verbose_name = "knowledge group membership"
        verbose_name_plural = "knowledge group memberships"
        constraints = [
            models.UniqueConstraint(fields=["user", "group"], name="uniq_group_membership")
        ]

    def __str__(self) -> str:
        return f"{self.user} in {self.group} ({self.role})"
