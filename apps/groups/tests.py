from django.test import TestCase

from apps.accounts.models import User
from apps.groups.models import Group, GroupMembership


class GroupTests(TestCase):
    def setUp(self):
        self.alice = User.objects.create_user("alice", "alice@example.com", "pw")
        self.bob = User.objects.create_user("bob", "bob@example.com", "pw")
        self.group = Group.objects.create(name="Engineering")

    def test_membership_is_unique_per_user(self):
        from django.db import IntegrityError

        GroupMembership.objects.create(user=self.alice, group=self.group)
        with self.assertRaises(IntegrityError):
            GroupMembership.objects.create(user=self.alice, group=self.group)

    def test_users_and_groups_separate(self):
        GroupMembership.objects.create(user=self.alice, group=self.group)
        other = Group.objects.create(name="Platform")
        GroupMembership.objects.create(user=self.bob, group=other)
        self.assertEqual(list(self.alice.group_memberships.values_list("group_id", flat=True)), [self.group.pk])
        self.assertEqual(list(self.bob.group_memberships.values_list("group_id", flat=True)), [other.pk])

    def test_user_group_ids_helper(self):
        GroupMembership.objects.create(user=self.alice, group=self.group)
        self.assertEqual(self.alice.group_ids, [self.group.pk])

    def test_role_default_is_member(self):
        membership = GroupMembership.objects.create(user=self.alice, group=self.group)
        self.assertEqual(membership.role, GroupMembership.Role.MEMBER)

    def test_deleting_group_removes_memberships(self):
        GroupMembership.objects.create(user=self.alice, group=self.group)
        self.group.delete()
        self.assertEqual(GroupMembership.objects.filter(user=self.alice).count(), 0)