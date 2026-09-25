from django.test import TestCase

from apps.accounts.models import User
from apps.documents.services import DocumentService
from apps.groups.models import Group, GroupMembership
from apps.permissions.constants import Effect, Permission, SubjectType
from apps.permissions.services import PermissionService
from apps.workspaces.services import ProjectService, WorkspaceService


class PermissionEngineTests(TestCase):
    def setUp(self):
        self.alice = User.objects.create_user("alice", "alice@example.com", "pw")
        self.bob = User.objects.create_user("bob", "bob@example.com", "pw")
        self.workspace = WorkspaceService.create(name="Company", created_by=self.alice)

    def test_creator_gets_admin(self):
        self.assertTrue(
            PermissionService.check(self.alice, self.workspace.resource, Permission.ADMIN)
        )

    def test_unrelated_user_is_denied(self):
        self.assertFalse(
            PermissionService.check(self.bob, self.workspace.resource, Permission.READ)
        )

    def test_group_grant_is_inherited_by_documents(self):
        group = Group.objects.create(name="Engineering")
        GroupMembership.objects.create(user=self.bob, group=group)
        PermissionService.grant(
            self.workspace.resource,
            subject_type=SubjectType.GROUP,
            subject_id=group.id,
            permission=Permission.READ,
        )
        project = ProjectService.create(workspace=self.workspace, name="Azure", created_by=self.alice)
        document = DocumentService.create(
            workspace=self.workspace, project=project, title="Deploy", content="# Deploy"
        )

        self.assertTrue(PermissionService.check(self.bob, document.resource, Permission.READ))
        self.assertFalse(PermissionService.check(self.bob, document.resource, Permission.WRITE))

    def test_explicit_deny_on_child_overrides_inherited_allow(self):
        group = Group.objects.create(name="Engineering")
        GroupMembership.objects.create(user=self.bob, group=group)
        PermissionService.grant(
            self.workspace.resource,
            subject_type=SubjectType.GROUP,
            subject_id=group.id,
            permission=Permission.READ,
        )
        project = ProjectService.create(workspace=self.workspace, name="Azure", created_by=self.alice)
        document = DocumentService.create(
            workspace=self.workspace, project=project, title="Secret", content="x"
        )
        PermissionService.grant(
            project.resource,
            subject_type=SubjectType.GROUP,
            subject_id=group.id,
            permission=Permission.READ,
            effect=Effect.DENY,
        )

        self.assertFalse(PermissionService.check(self.bob, document.resource, Permission.READ))
        # The owner still has full access.
        self.assertTrue(PermissionService.check(self.alice, document.resource, Permission.ADMIN))

    def test_non_inheriting_grant_does_not_leak_to_children(self):
        group = Group.objects.create(name="Auditors")
        GroupMembership.objects.create(user=self.bob, group=group)
        PermissionService.grant(
            self.workspace.resource,
            subject_type=SubjectType.GROUP,
            subject_id=group.id,
            permission=Permission.READ,
            inherit=False,
        )
        project = ProjectService.create(workspace=self.workspace, name="Azure", created_by=self.alice)

        self.assertTrue(PermissionService.check(self.bob, self.workspace.resource, Permission.READ))
        self.assertFalse(PermissionService.check(self.bob, project.resource, Permission.READ))
