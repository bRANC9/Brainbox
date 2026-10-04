"""The access model itself: who can see what, and who may widen it.

These tests are the executable form of the decisions in the design note
"Brainbox — jogosultsági modell újratervezése". The invariants that matter:

* a superuser has no implicit content access, and the only way in is the
  explicit, audited takeover;
* ownership is a field, it is transferred explicitly, and it is not a
  shareable privilege;
* a DENY is absolute - a narrower ALLOW deeper in the tree cannot re-open it;
* sharing is bounded: inside the workspace, and only towards the audience that
  already exists there (plus the caller's own work group);
* a Personal workspace has exactly one holder and is never shareable;
* a folder carries its own ACL, its ancestors stay visible as a trail, and its
  siblings do not.
"""

from django.core.exceptions import PermissionDenied
from django.test import TestCase, override_settings

from apps.accounts.models import User
from apps.documents.folders import create_folder, delete_folder, move_folder
from apps.documents.services import DocumentService
from apps.groups.models import Group, GroupMembership
from apps.permissions.constants import Effect, Permission, SubjectType
from apps.permissions.services import PermissionService
from apps.workspaces.models import Workspace, WorkspaceKind
from apps.workspaces.ownership import OwnershipService
from apps.workspaces.personal import PersonalWorkspaceService
from apps.workspaces.services import ProjectService, WorkspaceService


class SuperuserTests(TestCase):
    """A platform admin must not be a back door into somebody's notes."""

    def setUp(self):
        self.root = User.objects.create_user("root", "root@example.com", "pw")
        self.root.is_superuser = True
        self.root.is_staff = True
        self.root.save()
        self.alice = User.objects.create_user("alice", "alice@example.com", "pw")
        self.workspace = WorkspaceService.create(name="Company", created_by=self.alice)
        self.project = ProjectService.create(
            workspace=self.workspace, name="Secret", created_by=self.alice
        )
        self.document = DocumentService.create(
            workspace=self.workspace,
            project=self.project,
            title="Salary",
            content="# Salary\n",
            created_by=self.alice,
        )

    def test_superuser_is_denied_everything_without_a_grant(self):
        for resource in (self.workspace.resource, self.project.resource, self.document.resource):
            for permission in (Permission.READ, Permission.WRITE, Permission.DELETE, Permission.ADMIN):
                self.assertFalse(
                    PermissionService.check(self.root, resource, permission),
                    f"superuser must not pass {permission} on {resource}",
                )

    def test_superuser_is_not_listed_as_allowed(self):
        ids = [self.workspace.resource_id, self.document.resource_id]
        self.assertEqual(PermissionService.allowed_resource_ids(self.root, ids, Permission.READ), [])

    def test_takeover_is_the_only_way_in_and_is_audited(self):
        self.assertTrue(PermissionService.can_take_over(self.root, self.workspace.resource))
        OwnershipService.take_over(resource=self.workspace.resource, actor=self.root)
        # An ACL entry, inherited by the subtree - not a special case.
        self.assertTrue(
            PermissionService.check(self.root, self.document.resource, Permission.ADMIN)
        )
        # The owner is untouched, so this can never become a way to lock the
        # owner out of their own workspace.
        self.workspace.refresh_from_db()
        self.assertEqual(self.workspace.owner, self.alice)
        from apps.audit.models import AuditAction, AuditEvent

        self.assertTrue(
            AuditEvent.objects.filter(
                action=AuditAction.CHANGE_PERMISSION, detail__type="takeover"
            ).exists()
        )

    def test_takeover_does_not_grant_the_deny_instrument(self):
        OwnershipService.take_over(resource=self.workspace.resource, actor=self.root)
        self.assertFalse(
            PermissionService.can_grant(
                self.root, self.project.resource, Permission.READ, Effect.DENY
            )
        )

    def test_no_takeover_blocks_the_superuser(self):
        self.workspace.resource.no_takeover = True
        self.workspace.resource.save(update_fields=["no_takeover"])
        self.assertFalse(PermissionService.can_take_over(self.root, self.workspace.resource))
        with self.assertRaises(PermissionDenied):
            OwnershipService.take_over(resource=self.workspace.resource, actor=self.root)

    def test_takeover_is_not_offered_on_what_you_already_administer(self):
        """Taking over something you own is meaningless - and it was being offered."""
        self.assertTrue(PermissionService.can_take_over(self.root, self.workspace.resource))
        OwnershipService.take_over(resource=self.workspace.resource, actor=self.root)
        self.assertFalse(
            PermissionService.can_take_over(self.root, self.workspace.resource),
            "the superuser now holds an explicit ADMIN entry, so there is nothing to take over",
        )
        self.assertFalse(PermissionService.can_take_over(self.alice, self.workspace.resource))

    def test_takeover_stays_available_for_a_sibling_the_caller_cannot_reach(self):
        other = WorkspaceService.create(name="Zárt", created_by=self.alice)
        self.assertTrue(PermissionService.can_take_over(self.root, other.resource))

    def test_no_takeover_is_inherited_by_the_subtree(self):
        self.workspace.resource.no_takeover = True
        self.workspace.resource.save(update_fields=["no_takeover"])
        self.assertFalse(PermissionService.can_take_over(self.root, self.document.resource))

    def test_non_superuser_cannot_take_over(self):
        self.assertFalse(PermissionService.can_take_over(self.alice, self.workspace.resource))

    @override_settings(BRAINBOX_SUPERUSER_BYPASS=True)
    def test_rollout_flag_restores_the_old_behaviour(self):
        self.assertTrue(
            PermissionService.check(self.root, self.workspace.resource, Permission.ADMIN)
        )
        self.assertEqual(
            PermissionService.allowed_resource_ids(
                self.root, [self.workspace.resource_id], Permission.READ
            ),
            [self.workspace.resource_id],
        )


class AbsoluteDenyTests(TestCase):
    def setUp(self):
        self.alice = User.objects.create_user("alice", "alice@example.com", "pw")
        self.bob = User.objects.create_user("bob", "bob@example.com", "pw")
        self.workspace = WorkspaceService.create(name="Company", created_by=self.alice)
        self.group = Group.objects.create(name="Engineering")
        GroupMembership.objects.create(user=self.bob, group=self.group)

    def test_narrow_allow_under_a_broad_deny_stays_closed(self):
        """A grant on a deep folder must not re-open what a workspace DENY closed.

        The old engine stopped at the first level that produced a decision, so a
        folder-level ALLOW beat a workspace-level DENY. That is exactly backwards
        for "private until shared".
        """
        PermissionService.grant(
            self.workspace.resource,
            subject_type=SubjectType.GROUP,
            subject_id=self.group.id,
            permission=Permission.READ,
            effect=Effect.DENY,
            created_by=self.alice,
        )
        project = ProjectService.create(
            workspace=self.workspace, name="Deploy", created_by=self.alice
        )
        folder = create_folder(workspace=self.workspace, project=project, path="runbooks")
        PermissionService.grant(
            folder.resource,
            subject_type=SubjectType.USER,
            subject_id=self.bob.id,
            permission=Permission.READ,
            created_by=self.alice,
        )
        self.assertFalse(
            PermissionService.check(self.bob, folder.resource, Permission.READ),
            "a narrow ALLOW must not override an ancestor DENY",
        )

    def test_deny_on_a_child_still_beats_an_inherited_allow(self):
        PermissionService.grant(
            self.workspace.resource,
            subject_type=SubjectType.GROUP,
            subject_id=self.group.id,
            permission=Permission.WRITE,
            created_by=self.alice,
        )
        project = ProjectService.create(
            workspace=self.workspace, name="Deploy", created_by=self.alice
        )
        PermissionService.grant(
            project.resource,
            subject_type=SubjectType.GROUP,
            subject_id=self.group.id,
            permission=Permission.READ,
            effect=Effect.DENY,
            created_by=self.alice,
        )
        self.assertFalse(PermissionService.check(self.bob, project.resource, Permission.WRITE))


class WorkspaceAclOwnershipTests(TestCase):
    """On a *workspace* only the owner answers 'who is in here'."""

    def setUp(self):
        self.alice = User.objects.create_user("alice", "alice@example.com", "pw")
        self.coadmin = User.objects.create_user("coadmin", "co@example.com", "pw")
        self.outsider = User.objects.create_user("outsider", "out@example.com", "pw")
        self.workspace = WorkspaceService.create(name="Ecoform", created_by=self.alice)
        self.leaders = Group.objects.create(name="Vezetők")
        GroupMembership.objects.create(user=self.coadmin, group=self.leaders)
        PermissionService.grant(
            self.workspace.resource,
            subject_type=SubjectType.GROUP,
            subject_id=self.leaders.id,
            permission=Permission.ADMIN,
            created_by=self.alice,
        )
        self.project = ProjectService.create(
            workspace=self.workspace, name="Fejlesztői rész", created_by=self.alice
        )

    def test_coadmin_cannot_change_the_workspace_acl(self):
        self.assertFalse(PermissionService.can_manage_acl(self.coadmin, self.workspace.resource))
        with self.assertRaises(PermissionDenied):
            PermissionService.grant(
                self.workspace.resource,
                subject_type=SubjectType.USER,
                subject_id=self.outsider.id,
                permission=Permission.READ,
                created_by=self.coadmin,
            )

    def test_owner_can_change_the_workspace_acl(self):
        self.assertTrue(PermissionService.can_manage_acl(self.alice, self.workspace.resource))
        PermissionService.grant(
            self.workspace.resource,
            subject_type=SubjectType.USER,
            subject_id=self.outsider.id,
            permission=Permission.READ,
            created_by=self.alice,
        )
        self.assertTrue(
            PermissionService.check(self.outsider, self.workspace.resource, Permission.READ)
        )

    def test_coadmin_can_manage_acl_inside_the_workspace(self):
        self.assertTrue(PermissionService.can_manage_acl(self.coadmin, self.project.resource))

    def test_a_deny_needs_the_owner_even_inside(self):
        self.assertFalse(
            PermissionService.can_grant(
                self.coadmin, self.project.resource, Permission.READ, Effect.DENY
            )
        )
        self.assertTrue(
            PermissionService.can_grant(
                self.alice, self.project.resource, Permission.READ, Effect.DENY
            )
        )

    def test_a_takeover_entry_lets_a_superuser_manage_the_workspace(self):
        root = User.objects.create_user("root", "root@example.com", "pw")
        root.is_superuser = True
        root.save()
        OwnershipService.take_over(resource=self.workspace.resource, actor=root)
        self.assertTrue(PermissionService.can_manage_acl(root, self.workspace.resource))


class SharingPoolTests(TestCase):
    """A delegated admin sees the workspace audience, not the company."""

    def setUp(self):
        self.alice = User.objects.create_user("alice", "alice@example.com", "pw")
        self.writer = User.objects.create_user("writer", "writer@example.com", "pw")
        self.colleague = User.objects.create_user("colleague", "col@example.com", "pw")
        self.outsider = User.objects.create_user("outsider", "out@example.com", "pw")
        self.workspace = WorkspaceService.create(name="Ecoform", created_by=self.alice)
        self.project = ProjectService.create(
            workspace=self.workspace, name="Fejlesztői rész", created_by=self.alice
        )
        PermissionService.grant(
            self.workspace.resource,
            subject_type=SubjectType.USER,
            subject_id=self.writer.id,
            permission=Permission.WRITE,
            created_by=self.alice,
        )
        self.team = Group.objects.create(name="Fejlesztő csapat")
        GroupMembership.objects.create(user=self.writer, group=self.team, role="manager")
        GroupMembership.objects.create(user=self.colleague, group=self.team)

    def _pool(self, user, resource):
        return PermissionService.grantable_subjects(user, resource)

    def test_owner_sees_the_whole_directory(self):
        users, groups = self._pool(self.alice, self.workspace.resource)
        self.assertIn(self.outsider.id, users)
        self.assertTrue(len(users) >= 4)

    def test_delegate_sees_their_work_group_and_the_existing_audience(self):
        users, groups = self._pool(self.writer, self.project.resource)
        self.assertIn(self.colleague.id, users, "a colleague in my own group is shareable")
        self.assertIn(self.alice.id, users, "the existing audience is shareable")
        self.assertIn(self.team.id, groups)
        self.assertIn(self.writer.id, users, "I can always keep myself")

    def test_delegate_cannot_see_someone_outside(self):
        users, _ = self._pool(self.writer, self.project.resource)
        self.assertNotIn(self.outsider.id, users)

    def test_delegate_cannot_grant_to_someone_outside(self):
        with self.assertRaises(PermissionDenied):
            PermissionService.grant(
                self.project.resource,
                subject_type=SubjectType.USER,
                subject_id=self.outsider.id,
                permission=Permission.READ,
                created_by=self.writer,
            )
        self.assertFalse(
            PermissionService.check(self.outsider, self.project.resource, Permission.READ)
        )

    def test_a_reader_cannot_share(self):
        user_ids, group_ids = PermissionService.grantable_subjects(
            self.outsider, self.project.resource
        )
        self.assertEqual((user_ids, group_ids), (set(), set()))

    def test_sharing_is_impossible_outside_any_workspace(self):
        """A resource with no workspace ancestor is outside every sharing scope."""
        from apps.resources.models import Resource, ResourceType

        loose = Resource.objects.create(
            resource_type=ResourceType.SECRET, name="PAT", created_by=self.alice
        )
        self.assertIsNone(loose.workspace_resource())
        users, groups = PermissionService.grantable_subjects(self.writer, loose)
        self.assertEqual((users, groups), (set(), set()))
        self.assertFalse(
            PermissionService.can_grant(self.writer, loose, Permission.READ)
        )


class GrantCeilingTests(TestCase):
    def setUp(self):
        self.alice = User.objects.create_user("alice", "alice@example.com", "pw")
        self.writer = User.objects.create_user("writer", "writer@example.com", "pw")
        self.workspace = WorkspaceService.create(name="Company", created_by=self.alice)
        PermissionService.grant(
            self.workspace.resource,
            subject_type=SubjectType.USER,
            subject_id=self.writer.id,
            permission=Permission.WRITE,
            created_by=self.alice,
        )

    def test_a_writer_may_share_read_only(self):
        self.assertTrue(
            PermissionService.can_grant(
                self.writer, self.workspace.resource, Permission.READ
            )
        )
        for permission in (Permission.WRITE, Permission.DELETE, Permission.ADMIN):
            self.assertFalse(
                PermissionService.can_grant(self.writer, self.workspace.resource, permission)
            )

    def test_grant_enforces_the_ceiling_even_when_called_directly(self):
        """The rule lives in the mutation, not in the three callers."""
        with self.assertRaises(PermissionDenied):
            PermissionService.grant(
                self.workspace.resource,
                subject_type=SubjectType.USER,
                subject_id=self.writer.id,
                permission=Permission.ADMIN,
                created_by=self.writer,
            )

    def test_revoke_needs_the_acl_manager(self):
        with self.assertRaises(PermissionDenied):
            PermissionService.revoke(
                self.workspace.resource,
                subject_type=SubjectType.USER,
                subject_id=self.alice.id,
                actor=self.writer,
            )
        self.assertTrue(
            PermissionService.revoke(
                self.workspace.resource,
                subject_type=SubjectType.USER,
                subject_id=self.alice.id,
                actor=self.alice,
            )
        )

    def test_max_grantable_matches_the_ceiling(self):
        self.assertEqual(
            PermissionService.max_grantable(self.alice, self.workspace.resource),
            Permission.ADMIN,
        )
        self.assertEqual(
            PermissionService.max_grantable(self.writer, self.workspace.resource),
            Permission.WRITE,
        )


class PersonalWorkspaceTests(TestCase):
    def setUp(self):
        self.alice = User.objects.create_user("alice", "alice@example.com", "pw")
        self.bob = User.objects.create_user("bob", "bob@example.com", "pw")
        self.team = Group.objects.create(name="Everyone")
        GroupMembership.objects.create(user=self.alice, group=self.team)
        GroupMembership.objects.create(user=self.bob, group=self.team)

    def test_provisioning_is_idempotent_and_per_user(self):
        first = PersonalWorkspaceService.get_or_create(self.alice)
        second = PersonalWorkspaceService.get_or_create(self.alice)
        self.assertEqual(first.pk, second.pk)
        other = PersonalWorkspaceService.get_or_create(self.bob)
        self.assertNotEqual(first.pk, other.pk)
        self.assertEqual(Workspace.objects.filter(kind=WorkspaceKind.PERSONAL).count(), 2)

    def test_personal_is_private_to_its_owner(self):
        mine = PersonalWorkspaceService.get_or_create(self.alice)
        self.assertTrue(PermissionService.check(self.alice, mine.resource, Permission.ADMIN))
        self.assertFalse(PermissionService.check(self.bob, mine.resource, Permission.READ))
        self.assertEqual(
            PermissionService.allowed_resource_ids(self.bob, [mine.resource_id], Permission.READ),
            [],
        )

    def test_a_personal_workspace_cannot_be_shared_with_a_group(self):
        mine = PersonalWorkspaceService.get_or_create(self.alice)
        with self.assertRaises(PermissionDenied):
            PermissionService.grant(
                mine.resource,
                subject_type=SubjectType.GROUP,
                subject_id=self.team.id,
                permission=Permission.READ,
                created_by=self.alice,
            )
        self.assertFalse(
            PermissionService.can_grant_to(self.alice, mine.resource, SubjectType.GROUP, self.team.id)
        )

    def test_a_personal_workspace_cannot_be_shared_with_another_user(self):
        mine = PersonalWorkspaceService.get_or_create(self.alice)
        with self.assertRaises(PermissionDenied):
            PermissionService.grant(
                mine.resource,
                subject_type=SubjectType.USER,
                subject_id=self.bob.id,
                permission=Permission.READ,
                created_by=self.alice,
            )

    def test_ownership_cannot_be_transferred_out_of_a_personal_workspace(self):
        from django.core.exceptions import ValidationError

        mine = PersonalWorkspaceService.get_or_create(self.alice)
        with self.assertRaises(ValidationError):
            OwnershipService.transfer(
                resource=mine.resource, new_owner=self.bob, actor=self.alice
            )

    def test_it_can_be_promoted_to_a_shared_workspace(self):
        mine = PersonalWorkspaceService.get_or_create(self.alice)
        OwnershipService.set_kind(
            workspace=mine, kind=WorkspaceKind.SHARED, actor=self.alice
        )
        mine.refresh_from_db()
        self.assertEqual(mine.kind, WorkspaceKind.SHARED)
        PermissionService.grant(
            mine.resource,
            subject_type=SubjectType.USER,
            subject_id=self.bob.id,
            permission=Permission.READ,
            created_by=self.alice,
        )
        self.assertTrue(PermissionService.check(self.bob, mine.resource, Permission.READ))

    def test_a_shared_workspace_cannot_be_silently_made_personal(self):
        from django.core.exceptions import ValidationError

        shared = WorkspaceService.create(name="Ecoform", created_by=self.alice)
        PermissionService.grant(
            shared.resource,
            subject_type=SubjectType.USER,
            subject_id=self.bob.id,
            permission=Permission.WRITE,
            created_by=self.alice,
        )
        with self.assertRaises(ValidationError):
            OwnershipService.set_kind(
                workspace=shared, kind=WorkspaceKind.PERSONAL, actor=self.alice
            )

    def test_provision_all_backfills_every_active_user(self):
        created = PersonalWorkspaceService.provision_all()
        self.assertEqual(created, 2)
        self.assertEqual(PersonalWorkspaceService.provision_all(), 0)


class OwnershipTransferTests(TestCase):
    def setUp(self):
        self.alice = User.objects.create_user("alice", "alice@example.com", "pw")
        self.bob = User.objects.create_user("bob", "bob@example.com", "pw")
        self.workspace = WorkspaceService.create(name="Ecoform", created_by=self.alice)
        self.project = ProjectService.create(
            workspace=self.workspace, name="Deploy", created_by=self.alice
        )

    def test_the_creator_is_the_default_owner(self):
        self.assertEqual(self.workspace.owner, self.alice)
        self.assertEqual(self.project.owner, self.alice)

    def test_transfer_moves_the_admin_grant(self):
        # A project created by somebody else has no grant of its own, so alice's
        # only way in is the workspace entry that the transfer revokes.
        bobs_project = ProjectService.create(
            workspace=self.workspace, name="Vezetői rész", created_by=self.bob
        )
        document = DocumentService.create(
            workspace=bobs_project.workspace,
            project=bobs_project,
            title="Deploy",
            content="# x",
            path="deploy.md",
            created_by=self.bob,
        )
        self.assertTrue(PermissionService.check(self.alice, bobs_project.resource, Permission.READ))
        OwnershipService.transfer(
            resource=self.workspace.resource, new_owner=self.bob, actor=self.alice
        )
        self.workspace.refresh_from_db()
        self.assertEqual(self.workspace.owner, self.bob)
        self.assertTrue(PermissionService.check(self.bob, self.workspace.resource, Permission.ADMIN))
        self.assertFalse(PermissionService.check(self.alice, self.workspace.resource, Permission.ADMIN))
        self.assertFalse(
            PermissionService.check(self.alice, document.resource, Permission.READ),
            "nothing reachable only through the revoked workspace grant survives",
        )
        # The project alice created keeps her grant: it is a separate object with
        # a separate ACL, so transfer is not a recursive eviction.
        self.assertTrue(PermissionService.check(self.alice, self.project.resource, Permission.ADMIN))

    def test_the_previous_owner_can_keep_a_weaker_grant(self):
        OwnershipService.transfer(
            resource=self.workspace.resource,
            new_owner=self.bob,
            actor=self.alice,
            keep_old_access=Permission.READ,
        )
        self.assertTrue(PermissionService.check(self.alice, self.workspace.resource, Permission.READ))
        self.assertFalse(
            PermissionService.check(self.alice, self.workspace.resource, Permission.WRITE)
        )
        self.assertEqual(
            PermissionService.max_grantable(self.alice, self.workspace.resource),
            Permission.READ,
        )

    def test_a_non_owner_cannot_transfer(self):
        self.assertFalse(OwnershipService.can_transfer(self.bob, self.workspace.resource))
        with self.assertRaises(PermissionDenied):
            OwnershipService.transfer(
                resource=self.workspace.resource, new_owner=self.bob, actor=self.bob
            )

    def test_a_transfer_is_audited_with_both_owners(self):
        OwnershipService.transfer(
            resource=self.workspace.resource, new_owner=self.bob, actor=self.alice
        )
        from apps.audit.models import AuditAction, AuditEvent

        event = AuditEvent.objects.filter(detail__type="ownership_transfer").latest("id")
        self.assertEqual(event.detail["from"], str(self.alice.id))
        self.assertEqual(event.detail["to"], str(self.bob.id))
        self.assertEqual(event.action, AuditAction.CHANGE_PERMISSION)

    def test_an_inactive_user_cannot_become_the_owner(self):
        from django.core.exceptions import ValidationError

        self.bob.is_active = False
        self.bob.save()
        with self.assertRaises(ValidationError):
            OwnershipService.transfer(
                resource=self.workspace.resource, new_owner=self.bob, actor=self.alice
            )

    def test_a_project_owner_outranks_the_workspace_owner_in_the_pool(self):
        OwnershipService.transfer(
            resource=self.project.resource, new_owner=self.bob, actor=self.alice
        )
        self.assertTrue(PermissionService.is_scope_owner(self.bob, self.project.resource))
        self.assertTrue(PermissionService.is_scope_owner(self.alice, self.workspace.resource))


class FolderAclTests(TestCase):
    """A folder is a permission-managed object, and the tree stays navigable."""

    def setUp(self):
        self.alice = User.objects.create_user("alice", "alice@example.com", "pw")
        self.bob = User.objects.create_user("bob", "bob@example.com", "pw")
        self.workspace = WorkspaceService.create(name="Company", created_by=self.alice)
        self.project = ProjectService.create(
            workspace=self.workspace, name="Ops", created_by=self.alice
        )
        self.folder = create_folder(
            workspace=self.workspace, project=self.project, path="a/b/c", created_by=self.alice
        )

    def _folder(self, path):
        from apps.documents.models import DocumentFolder

        return DocumentFolder.objects.get(workspace=self.workspace, project=self.project, path=path)

    def test_creating_a_nested_folder_builds_the_resource_chain(self):
        parent = self._folder("a")
        mid = self._folder("a/b")
        self.assertEqual(parent.resource.parent_id, self.project.resource_id)
        self.assertEqual(mid.resource.parent_id, parent.resource_id)
        self.assertEqual(self.folder.resource.parent_id, mid.resource_id)

    def test_access_on_a_deep_folder_reaches_its_contents_only(self):
        other = create_folder(
            workspace=self.workspace, project=self.project, path="other", created_by=self.alice
        )
        inside = DocumentService.create(
            workspace=self.workspace,
            project=self.project,
            title="Runbook",
            content="# x",
            path="a/b/c/runbook.md",
            created_by=self.alice,
        )
        outside = DocumentService.create(
            workspace=self.workspace,
            project=self.project,
            title="Other",
            content="# x",
            path="other/other.md",
            created_by=self.alice,
        )
        PermissionService.grant(
            self.folder.resource,
            subject_type=SubjectType.USER,
            subject_id=self.bob.id,
            permission=Permission.READ,
            created_by=self.alice,
        )
        self.assertTrue(PermissionService.check(self.bob, inside.resource, Permission.READ))
        self.assertFalse(PermissionService.check(self.bob, other.resource, Permission.READ))
        self.assertFalse(PermissionService.check(self.bob, outside.resource, Permission.READ))
        # The container itself is still closed: read on a folder is not admin.
        self.assertFalse(PermissionService.check(self.bob, self.project.resource, Permission.READ))

    def test_ancestors_are_visible_but_siblings_stay_hidden(self):
        for path in ("a", "a/b", "a/b/c", "a/b/x", "a/y"):
            create_folder(workspace=self.workspace, project=self.project, path=path)
        self.folder = self._folder("a/b/c")
        PermissionService.grant(
            self.folder.resource,
            subject_type=SubjectType.USER,
            subject_id=self.bob.id,
            permission=Permission.READ,
            created_by=self.alice,
        )
        ids = [
            self._folder(path).resource_id
            for path in ("a", "a/b", "a/b/c", "a/b/x", "a/y")
        ]
        visible = PermissionService.visible_resource_ids(self.bob, ids)
        readable = PermissionService.allowed_resource_ids(self.bob, ids, Permission.READ)
        self.assertEqual(len(visible), 3, "the granted folder plus its two ancestors")
        self.assertEqual(len(readable), 1, "only the granted folder is really readable")

    def test_moving_a_document_changes_the_access_it_inherits(self):
        """Otherwise a document would keep the permissions of where it was born."""
        tight = self.folder
        other = create_folder(
            workspace=self.workspace, project=self.project, path="public", created_by=self.alice
        )
        document = DocumentService.create(
            workspace=self.workspace,
            project=self.project,
            title="Runbook",
            content="# x",
            path="public/runbook.md",
            created_by=self.alice,
        )
        self.assertTrue(PermissionService.check(self.alice, document.resource, Permission.ADMIN))
        PermissionService.grant(
            other.resource,
            subject_type=SubjectType.USER,
            subject_id=self.bob.id,
            permission=Permission.READ,
            created_by=self.alice,
        )
        self.assertTrue(PermissionService.check(self.bob, document.resource, Permission.READ))

        DocumentService.move(document, "a/b/c/runbook.md", user=self.alice)
        document.refresh_from_db()
        self.assertEqual(
            document.resource.parent_id,
            self._folder("a/b/c").resource_id,
            "a move has to re-parent, not just rewrite the path",
        )
        self.assertFalse(
            PermissionService.check(self.bob, document.resource, Permission.READ),
            "moving into a closed folder must close the document",
        )
        self.assertTrue(PermissionService.is_personal(tight.resource) is False)

    def test_moving_a_folder_reparents_it(self):
        target = create_folder(
            workspace=self.workspace, project=self.project, path="target", created_by=self.alice
        )
        moved = self._folder("a/b")
        move_folder(folder=moved, new_parent=target, user=self.alice)
        moved.refresh_from_db()
        # A folder is a node now, so moving it moves the node: "a/b" under
        # "target" becomes "target/b", not "target/a/b". The old behaviour nested
        # the whole subpath, which is what made a rename rewrite every
        # descendant. Its own parent folder is untouched.
        self.assertEqual(moved.path, "target/b")
        self.assertEqual(moved.resource.parent_id, target.resource_id)
        self.assertEqual(moved.name, "b")
        # The parent keeps its identity, and its own container is unchanged.
        self.assertEqual(self._folder("a").path, "a")
        self.assertEqual(
            self._folder("a").resource.parent_id, self.project.resource_id
        )

    def test_deleting_a_folder_refuses_to_swallow_a_subtree_implicitly(self):
        """A Resource delete cascades, so the default has to refuse."""
        from django.core.exceptions import ValidationError

        from apps.documents.models import DocumentFolder
        from apps.resources.models import Resource

        parent = self._folder("a/b")
        child_id = self._folder("a/b/c").resource_id

        with self.assertRaises(ValidationError):
            delete_folder(folder=parent)
        self.assertTrue(DocumentFolder.objects.filter(path="a/b/c").exists())

        delete_folder(folder=parent, recursive=True)
        self.assertFalse(DocumentFolder.objects.filter(path="a/b").exists())
        self.assertFalse(DocumentFolder.objects.filter(path="a/b/c").exists())
        self.assertFalse(Resource.objects.filter(pk=child_id).exists())

    def test_the_chain_survives_a_recursive_delete_of_a_sibling_subtree(self):
        """A cascade must be scoped to the deleted node, not the whole scope."""
        from apps.documents.models import DocumentFolder
        from apps.resources.models import Resource

        # self.folder is the "a/b/c" node inside the subtree being deleted, so
        # the survivor has to be a sibling: same project, different branch.
        survivor = create_folder(
            workspace=self.workspace,
            project=self.project,
            path="runbooks",
            created_by=self.alice,
        )
        deep = create_folder(
            workspace=self.workspace,
            project=self.project,
            path="runbooks/2024",
            created_by=self.alice,
        )
        delete_folder(folder=self._folder("a/b"), recursive=True)

        self.assertFalse(DocumentFolder.objects.filter(path="a/b").exists())
        self.assertFalse(DocumentFolder.objects.filter(path="a/b/c").exists())
        self.assertTrue(DocumentFolder.objects.filter(pk=deep.pk).exists())
        self.assertTrue(Resource.objects.filter(pk=deep.resource_id).exists())
        self.assertEqual(deep.container_id, survivor.resource_id)


class BrowseReachabilityTests(TestCase):
    """A grant on a deep folder has to be *reachable*, not just valid.

    Otherwise "grant me Ecoform/Fejlesztoi resz/runbooks/2024" gives the person
    a document they can only find by search: the project page still 404s,
    because a plain READ check on the project says no.
    """

    def setUp(self):
        self.alice = User.objects.create_user("alice", "alice@example.com", "pw")
        self.bob = User.objects.create_user("bob", "bob@example.com", "pw")
        self.workspace = WorkspaceService.create(name="Ecoform", created_by=self.alice)
        self.project = ProjectService.create(
            workspace=self.workspace, name="Fejlesztői rész", created_by=self.alice
        )
        self.folder = create_folder(
            workspace=self.workspace, project=self.project, path="runbooks/2024",
            created_by=self.alice,
        )
        self.document = DocumentService.create(
            workspace=self.workspace,
            project=self.project,
            title="Éles runbook",
            content="# x",
            path="runbooks/2024/runbook.md",
            created_by=self.alice,
        )

    def test_no_grant_means_no_browse(self):
        self.assertFalse(PermissionService.can_browse(self.bob, self.project.resource))
        self.assertFalse(PermissionService.can_browse(self.bob, self.workspace.resource))

    def test_a_folder_grant_makes_the_containers_browsable(self):
        self.assertFalse(PermissionService.check(self.bob, self.project.resource, Permission.READ))
        PermissionService.grant(
            self.folder.resource,
            subject_type=SubjectType.USER,
            subject_id=self.bob.id,
            permission=Permission.READ,
            created_by=self.alice,
        )
        self.assertTrue(
            PermissionService.can_browse(self.bob, self.project.resource),
            "otherwise the trail that leads to the folder is never rendered",
        )
        self.assertTrue(PermissionService.can_browse(self.bob, self.workspace.resource))
        # Still not readable: the trail is navigation, not access.
        self.assertFalse(PermissionService.check(self.bob, self.project.resource, Permission.READ))

    def test_a_group_grant_counts_too(self):
        team = Group.objects.create(name="Runbook olvasók")
        GroupMembership.objects.create(user=self.bob, group=team)
        PermissionService.grant(
            self.folder.resource,
            subject_type=SubjectType.GROUP,
            subject_id=team.id,
            permission=Permission.READ,
            created_by=self.alice,
        )
        self.assertTrue(PermissionService.can_browse(self.bob, self.project.resource))

    def test_a_deny_below_does_not_open_the_container(self):
        team = Group.objects.create(name="Runbook olvasók")
        GroupMembership.objects.create(user=self.bob, group=team)
        PermissionService.grant(
            self.folder.resource,
            subject_type=SubjectType.GROUP,
            subject_id=team.id,
            permission=Permission.READ,
            created_by=self.alice,
        )
        # An ancestor DENY outranks it: the container must stay closed.
        PermissionService.grant(
            self.project.resource,
            subject_type=SubjectType.GROUP,
            subject_id=team.id,
            permission=Permission.READ,
            effect=Effect.DENY,
            created_by=self.alice,
        )
        self.assertFalse(PermissionService.can_browse(self.bob, self.project.resource))

def test_the_workspace_page_shows_the_trail_up_to_the_project(self):
        """A deep grant has to be reachable by clicking, not only by direct URL.

        The workspace page renders only the nodes whose container *is* the
        workspace - the project nodes - but computes visibility over every folder
        in the workspace, so a readable folder deep inside a project lights up the
        project node above it. Siblings that hold nothing readable stay hidden.
        """
        secret_project = ProjectService.create(
            workspace=self.workspace, name="Zárt", created_by=self.alice
        )
        DocumentService.create(
            workspace=self.workspace, project=secret_project, title="Zárt doksi",
            content="# x", path="zart.md", created_by=self.alice,
        )
        PermissionService.grant(
            self.folder.resource,
            subject_type=SubjectType.USER,
            subject_id=self.bob.id,
            permission=Permission.READ,
            created_by=self.alice,
        )
        from apps.web.views import _folder_tree_rows

        rows = _folder_tree_rows(self.workspace, None, self.bob)
        paths = {row.get("path") for row in rows if row["type"] == "dir"}
        # The project holding the grant shows up as a trail node...
        self.assertEqual(paths, {"Fejlesztői rész"})
        node = next(row for row in rows if row["type"] == "dir")
        self.assertFalse(node["can_read"], "it is a way in, not access")

        # ...and inside it, the full trail down to the document.
        project_rows = _folder_tree_rows(self.workspace, self.project, self.bob)
        project_paths = {row.get("path") for row in project_rows if row["type"] == "dir"}
        self.assertEqual(project_paths, {"runbooks", "runbooks/2024"})
        titles = {row["name"] for row in project_rows if row["type"] == "doc"}
        self.assertEqual(titles, {"Éles runbook"})
