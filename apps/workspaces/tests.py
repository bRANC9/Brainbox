from django.contrib.auth.models import AnonymousUser
from django.test import TestCase

from apps.accounts.models import User
from apps.permissions.constants import Permission
from apps.permissions.models import ResourceACL
from apps.permissions.services import PermissionService
from apps.resources.models import Resource

from .services import ProjectService, WorkspaceService


class OwnerlessCreationTests(TestCase):
    """Ownership is the implicit ADMIN grant, so no actor means no access.

    Without the guard these writes succeeded and produced a row with
    ``created_by=NULL`` and zero ACL entries -- an object that
    :class:`PermissionService` denies to everyone, its own creator included.
    """

    def setUp(self):
        self.alice = User.objects.create_user("alice", "alice@example.com", "pw")

    def test_workspace_refuses_missing_actor(self):
        with self.assertRaises(TypeError):
            WorkspaceService.create(name="NoActor")

    def test_workspace_refuses_none_actor(self):
        with self.assertRaises(ValueError):
            WorkspaceService.create(name="NoActor", created_by=None)
        self.assertEqual(Resource.objects.filter(resource_type="workspace").count(), 0)

    def test_workspace_refuses_anonymous_actor(self):
        with self.assertRaises(ValueError):
            WorkspaceService.create(name="NoActor", created_by=AnonymousUser())
        self.assertEqual(Resource.objects.filter(resource_type="workspace").count(), 0)

    def test_project_refuses_missing_actor(self):
        workspace = WorkspaceService.create(name="Acme", created_by=self.alice)
        with self.assertRaises(TypeError):
            ProjectService.create(workspace=workspace, name="NoActor")

    def test_project_refuses_none_actor(self):
        workspace = WorkspaceService.create(name="Acme", created_by=self.alice)
        with self.assertRaises(ValueError):
            ProjectService.create(workspace=workspace, name="NoActor", created_by=None)
        self.assertEqual(Resource.objects.filter(resource_type="project").count(), 0)
        self.assertEqual(
            ResourceACL.objects.filter(
                resource__resource_type="project"
            ).count(),
            0,
        )

    def test_project_refuses_anonymous_actor(self):
        workspace = WorkspaceService.create(name="Acme", created_by=self.alice)
        with self.assertRaises(ValueError):
            ProjectService.create(
                workspace=workspace, name="NoActor", created_by=AnonymousUser()
            )


class CreatorOwnershipTests(TestCase):
    def setUp(self):
        self.alice = User.objects.create_user("alice", "alice@example.com", "pw")

    def test_workspace_creator_gets_admin(self):
        workspace = WorkspaceService.create(name="Acme", created_by=self.alice)
        self.assertEqual(workspace.created_by, self.alice)
        self.assertTrue(
            PermissionService.check(self.alice, workspace.resource, Permission.ADMIN)
        )
        self.assertEqual(
            ResourceACL.objects.filter(resource=workspace.resource).count(), 1
        )

    def test_project_creator_gets_admin(self):
        workspace = WorkspaceService.create(name="Acme", created_by=self.alice)
        project = ProjectService.create(
            workspace=workspace, name="Azure", created_by=self.alice
        )
        self.assertEqual(project.created_by, self.alice)
        self.assertTrue(
            PermissionService.check(self.alice, project.resource, Permission.ADMIN)
        )
        self.assertEqual(
            ResourceACL.objects.filter(resource=project.resource).count(), 1
        )
