"""The audit/API-key project FKs point at Resources now, not at Project rows.

The whole reason that retarget is safe is one invariant: a Project's primary key
IS its Resource id, so every stored FK value keeps resolving. These tests pin
that invariant and the two behaviours that quietly depend on it - filtering audit
events by a Project instance (the value lookup uses .pk) and API-key scopes that
still narrow access by the same id.
"""

from django.contrib.auth import get_user_model
from django.test import TestCase

from apps.accounts.models import ApiKeyScope
from apps.accounts.services import ApiKeyService
from apps.audit.models import AuditAction, AuditEvent
from apps.audit.services import AuditService
from apps.documents.services import DocumentService
from apps.permissions.constants import Effect, Permission, SubjectType
from apps.permissions.services import PermissionService
from apps.resources.models import Resource, ResourceType
from apps.workspaces.services import ProjectService, WorkspaceService

User = get_user_model()


class ProjectResourceIdentityTests(TestCase):
    def setUp(self):
        self.alice = User.objects.create_user("alice", "a@x.test", "pw")
        self.bob = User.objects.create_user("bob", "b@x.test", "pw")
        self.company = WorkspaceService.create(name="Company", created_by=self.alice)
        self.project = ProjectService.create(
            workspace=self.company, name="Deploy", created_by=self.alice
        )

    def test_project_pk_is_its_resource_id(self):
        """The invariant the retarget rests on. If this breaks, values break."""
        self.assertEqual(self.project.pk, self.project.resource_id)

    def test_a_document_scope_id_and_its_project_resource_agree(self):
        document = DocumentService.create(
            workspace=self.company,
            project=self.project,
            title="Readme",
            content="# x",
            path="readme.md",
            created_by=self.alice,
        )
        self.assertEqual(
            document.resource.project_resource().id, self.project.resource_id
        )


class AuditProjectPointerTests(TestCase):
    def setUp(self):
        self.alice = User.objects.create_user("alice2", "a2@x.test", "pw")
        self.company = WorkspaceService.create(name="Company2", created_by=self.alice)
        self.project = ProjectService.create(
            workspace=self.company, name="Deploy", created_by=self.alice
        )

    def test_log_accepts_a_project_row_and_stores_its_resource(self):
        event = AuditService.log(AuditAction.CREATE, user=self.alice, project=self.project)
        self.assertIsInstance(event.project, Resource)
        self.assertEqual(event.project_id, self.project.resource_id)
        # Re-read from the DB: the stored column holds the resource id, and the
        # event pk is a random UUID, so order by timestamp, never by id.
        stored = AuditEvent.objects.order_by("-timestamp").first()
        self.assertEqual(stored.project_id, self.project.resource_id)

    def test_filtering_by_a_project_instance_raises_and_the_id_works(self):
        """The one behaviour change of the retarget, pinned down.

        A ``project=`` **filter** no longer accepts a Project instance - Django
        requires the related model's instance, or a raw pk, because the FK now
        points at ``Resource``. Nothing in the product filters events by project
        (the audit trail is read through the dashboard, not by project id), so
        the change is contained - but passing a raw ``.pk``/UUID keeps working,
        and that is what any future caller must do.
        """
        AuditService.log(AuditAction.CREATE, user=self.alice, project=self.project)
        with self.assertRaises(ValueError):
            AuditEvent.objects.filter(project=self.project).count()
        # Two events carry this id: creating the project is itself audited, and
        # that audit already stored the resource id rather than a Project row.
        self.assertEqual(AuditEvent.objects.filter(project=self.project.pk).count(), 2)
        self.assertEqual(
            AuditEvent.objects.filter(project=self.project.resource).count(), 2
        )

    def test_storing_a_folder_node_records_the_shared_resource(self):
        """Projects are folder nodes now; passing the node lands on the same id."""
        from apps.documents.models import DocumentFolder

        node = DocumentFolder.objects.get(resource_id=self.project.resource_id)
        event = AuditService.log(AuditAction.UPDATE, user=self.alice, project=node)
        self.assertEqual(event.project_id, self.project.resource_id)


class ApiKeyScopePointerTests(TestCase):
    def setUp(self):
        self.alice = User.objects.create_user("alice3", "a3@x.test", "pw")
        self.bob = User.objects.create_user("bob3", "b3@x.test", "pw")
        self.company = WorkspaceService.create(name="Company3", created_by=self.alice)
        self.project = ProjectService.create(
            workspace=self.company, name="Deploy", created_by=self.alice
        )
        self.document = DocumentService.create(
            workspace=self.company,
            project=self.project,
            title="Runbook",
            content="# x",
            path="runbook.md",
            created_by=self.alice,
        )
        PermissionService.grant(
            self.project.resource,
            subject_type=SubjectType.USER,
            subject_id=self.bob.id,
            permission=Permission.READ,
            created_by=self.alice,
        )

    def test_a_scope_created_from_a_project_id_narrows_by_the_same_uuid(self):
        _, raw_key = ApiKeyService.create(
            user=self.bob,
            name="project-only",
            scopes=[
                {
                    "project": self.project.resource,
                    "permission": Permission.READ,
                    "effect": Effect.ALLOW,
                }
            ],
        )
        scope = ApiKeyScope.objects.get(api_key__name="project-only")
        self.assertEqual(scope.project_id, self.project.resource_id)
        self.assertTrue(scope.applies_to(None, self.project.resource_id))
        self.assertFalse(scope.applies_to(None, None))
        self.assertTrue(
            PermissionService.check(self.bob, self.document.resource, Permission.READ, api_key=scope.api_key)
        )

    def test_the_scope_input_accepts_the_same_id_a_project_row_would_give(self):
        from apps.api.serializers import ApiKeyScopeInputSerializer

        field = ApiKeyScopeInputSerializer().get_fields()["project"]
        self.assertTrue(
            field.queryset.filter(pk=self.project.pk).exists(),
            "Project.pk and its Resource id are one value; clients keep sending it",
        )
        self.assertFalse(field.queryset.filter(resource_type=ResourceType.DOCUMENT).exists())
