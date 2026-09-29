import calendar as calmod
import tempfile

from django.test import TestCase, override_settings
from django.urls import reverse

from apps.accounts.models import ApiKey, User
from apps.documents.services import DocumentService
from apps.groups.models import Group, GroupMembership
from apps.permissions.constants import Permission
from apps.permissions.models import ResourceACL
from apps.permissions.services import PermissionService
from apps.workspaces.services import ProjectService, WorkspaceService


class ManagementUITests(TestCase):
    def setUp(self):
        self.alice = User.objects.create_user("alice", "alice@example.com", "pw")
        self.workspace = WorkspaceService.create(name="Company", created_by=self.alice)
        self.client.force_login(self.alice)

    def test_api_key_create_and_revoke(self):
        self.client.post(reverse("web:api_keys"), {"action": "create", "name": "laptop"})
        key = ApiKey.objects.get(user=self.alice, name="laptop")
        self.assertTrue(key.is_active)

        self.client.post(
            reverse("web:api_keys"), {"action": "revoke", "key_id": str(key.pk)}
        )
        key.refresh_from_db()
        self.assertFalse(key.is_active)

    def test_permission_grant_and_revoke(self):
        bob = User.objects.create_user("bob", "bob@example.com", "pw")
        url = reverse("web:resource_permissions", args=[self.workspace.resource_id])

        self.client.post(
            url,
            {
                "action": "grant",
                "subject_type": "user",
                "subject_id": "bob",
                "permission": "read",
                "effect": "allow",
                "inherit": "on",
            },
        )
        self.assertTrue(PermissionService.check(bob, self.workspace.resource, Permission.READ))

        self.client.post(
            url,
            {
                "action": "revoke",
                "subject_type": "user",
                "subject_id": str(bob.id),
                "permission": "read",
            },
        )
        self.assertFalse(PermissionService.check(bob, self.workspace.resource, Permission.READ))
        self.assertFalse(
            ResourceACL.objects.filter(resource=self.workspace.resource, subject_id=bob.id).exists()
        )

    def test_permission_page_forbidden_without_admin(self):
        bob = User.objects.create_user("bob2", "bob2@example.com", "pw")
        self.client.force_login(bob)
        response = self.client.get(
            reverse("web:resource_permissions", args=[self.workspace.resource_id])
        )
        self.assertEqual(response.status_code, 403)

    def test_audit_dashboard_renders(self):
        response = self.client.get(reverse("web:audit_dashboard"))
        self.assertEqual(response.status_code, 200)

    def test_groups_requires_staff(self):
        response = self.client.get(reverse("web:groups_admin"))
        self.assertEqual(response.status_code, 403)


class AccessPanelTests(TestCase):
    def setUp(self):
        self.alice = User.objects.create_user("alice", "alice@example.com", "pw")
        self.writer = User.objects.create_user("writer", "writer@example.com", "pw")
        self.bob = User.objects.create_user("bob", "bob@example.com", "pw")
        self.workspace = WorkspaceService.create(name="Company", created_by=self.alice)
        # writer gets write (not admin) on the workspace
        from apps.permissions.constants import Permission, SubjectType
        from apps.permissions.services import PermissionService

        PermissionService.grant(
            self.workspace.resource,
            subject_type=SubjectType.USER,
            subject_id=self.writer.id,
            permission=Permission.WRITE,
        )

    def test_writer_can_open_access_panel(self):
        self.client.force_login(self.writer)
        response = self.client.get(reverse("web:resource_permissions", args=[self.workspace.resource_id]))
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.context["can_share"])
        self.assertFalse(response.context["can_admin"])

    def test_writer_can_share_read_but_not_write(self):
        from apps.permissions.constants import Effect, Permission
        from apps.permissions.services import PermissionService

        self.client.force_login(self.writer)
        url = reverse("web:resource_permissions", args=[self.workspace.resource_id])
        self.client.post(
            url,
            {"action": "grant", "subject_id": f"user:{self.bob.id}", "permission": "read", "effect": "allow", "inherit": "on"},
            follow=True,
        )
        self.assertTrue(PermissionService.check(self.bob, self.workspace.resource, Permission.READ))

        # writer tries to escalate to write -> rejected
        self.client.post(
            url,
            {"action": "grant", "subject_id": f"user:{self.bob.id}", "permission": "write", "effect": "allow", "inherit": "on"},
            follow=True,
        )
        self.assertFalse(PermissionService.check(self.bob, self.workspace.resource, Permission.WRITE))
        # deny is admin-only
        self.client.post(
            url,
            {"action": "grant", "subject_id": f"user:{self.bob.id}", "permission": "read", "effect": "deny"},
            follow=True,
        )
        # a deny from a writer must not have created a deny entry
        from apps.permissions.models import ResourceACL

        self.assertFalse(
            ResourceACL.objects.filter(
                resource=self.workspace.resource, subject_id=self.bob.id, effect=Effect.DENY
            ).exists()
        )

    def test_admin_can_create_group_and_grant(self):
        from apps.groups.models import Group
        from apps.permissions.constants import SubjectType
        from apps.permissions.models import ResourceACL

        self.client.force_login(self.alice)
        url = reverse("web:resource_permissions", args=[self.workspace.resource_id])
        self.client.post(url, {"action": "create_group", "group_name": "Platform"}, follow=True)
        group = Group.objects.get(name="Platform")
        self.assertTrue(
            ResourceACL.objects.filter(
                resource=self.workspace.resource,
                subject_type=SubjectType.GROUP,
                subject_id=group.id,
            ).exists()
        )

    def test_directory_search_lists_users(self):
        self.client.force_login(self.alice)
        response = self.client.get(
            reverse("web:resource_permissions", args=[self.workspace.resource_id]) + "?q=bob"
        )
        labels = [row["label"] for row in response.context["search"]["users"]]
        self.assertTrue(any("bob" in label for label in labels))


@override_settings(KNOWLEDGE_DATA_ROOT=tempfile.mkdtemp())
class AccessPanelInlineRegressionTests(TestCase):
    """The ACL panel is included on the workspace and project pages too.

    It used to be handed `access_can_admin` / `access_entries` while the shared
    partial reads `can_admin` / `entry_rows`, so the owner saw an empty table and
    a "you have no write access" note on a resource they administer.
    """

    def setUp(self):
        self.alice = User.objects.create_user("alice", "alice@example.com", "pw")
        self.bob = User.objects.create_user("bob", "bob@example.com", "pw")
        self.workspace = WorkspaceService.create(name="Company", created_by=self.alice)
        self.project = ProjectService.create(
            workspace=self.workspace, name="Alpha", created_by=self.alice
        )
        self.client.force_login(self.alice)

    def test_workspace_page_renders_the_panel_for_the_owner(self):
        response = self.client.get(
            reverse("web:workspace_detail", args=[self.workspace.slug])
        )
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.context["can_admin"])
        self.assertTrue(response.context["can_share"])
        self.assertNotIn("nincs írási jogod", response.content.decode())

    def test_project_page_renders_the_panel_for_the_owner(self):
        response = self.client.get(
            reverse("web:project_detail", args=[self.workspace.slug, self.project.slug])
        )
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.context["can_admin"])
        self.assertNotIn("nincs írási jogod", response.content.decode())

    def test_workspace_page_lists_the_owner_acl_entry(self):
        response = self.client.get(
            reverse("web:workspace_detail", args=[self.workspace.slug])
        )
        self.assertEqual(
            [row["label"] for row in response.context["entry_rows"]], ["alice"]
        )

    def test_panel_lists_every_user_without_a_search(self):
        """An empty query must offer the whole directory, not an empty select."""
        User.objects.create_user("carol", "carol@example.com", "pw")
        response = self.client.get(
            reverse("web:resource_permissions", args=[self.workspace.resource_id])
        )
        labels = [row["label"] for row in response.context["search"]["users"]]
        self.assertEqual(len(labels), User.objects.count())
        for expected in ("alice", "bob", "carol"):
            self.assertTrue(
                any(expected in label for label in labels), f"{expected} missing from {labels}"
            )

    def test_panel_renders_the_allow_effect_option_once(self):
        """A duplicated 'Engedélyez' option shipped for admins."""
        html = self.client.get(
            reverse("web:resource_permissions", args=[self.workspace.resource_id])
        ).content.decode()
        self.assertEqual(html.count('<option value="allow">Engedélyez</option>'), 1)

    def test_project_page_links_to_the_permissions_page(self):
        response = self.client.get(
            reverse("web:project_detail", args=[self.workspace.slug, self.project.slug])
        )
        url = reverse("web:resource_permissions", args=[self.project.resource_id])
        self.assertIn(url, response.content.decode())


class GroupMemberPickerTests(TestCase):
    """`Add member` is a dropdown of every non-member, not a free-text username."""

    def setUp(self):
        self.staff = User.objects.create_user(
            "root", "root@example.com", "pw", is_staff=True, is_superuser=True
        )
        self.alice = User.objects.create_user("alice", "alice@example.com", "pw")
        self.bob = User.objects.create_user("bob", "bob@example.com", "pw")
        self.group = Group.objects.create(name="Platform", created_by=self.staff)
        self.client.force_login(self.staff)

    def test_picker_offers_every_non_member(self):
        GroupMembership.objects.create(user=self.bob, group=self.group)
        response = self.client.get(reverse("web:groups_admin"))
        candidates = response.context["groups"][0].candidates
        labels = [row["label"] for row in candidates]
        self.assertTrue(any("alice" in label for label in labels))
        self.assertFalse(any("bob" in label for label in labels), labels)

    def test_picker_is_empty_when_everyone_is_a_member(self):
        for user in (self.staff, self.alice, self.bob):
            GroupMembership.objects.create(user=user, group=self.group)
        response = self.client.get(reverse("web:groups_admin"))
        self.assertEqual(response.context["groups"][0].candidates, [])
        self.assertContains(response, "already a member")

    def test_add_member_by_picker(self):
        self.client.post(
            reverse("web:groups_admin"),
            {
                "action": "add_member",
                "group_id": str(self.group.pk),
                "user_id": str(self.bob.pk),
                "role": "member",
            },
        )
        self.assertTrue(
            GroupMembership.objects.filter(user=self.bob, group=self.group).exists()
        )

    def test_add_member_reports_duplicate_instead_of_silently_reusing(self):
        GroupMembership.objects.create(user=self.bob, group=self.group)
        response = self.client.post(
            reverse("web:groups_admin"),
            {
                "action": "add_member",
                "group_id": str(self.group.pk),
                "user_id": str(self.bob.pk),
            },
            follow=True,
        )
        self.assertEqual(GroupMembership.objects.filter(group=self.group).count(), 1)
        self.assertContains(response, "already in")

    def test_add_member_rejects_an_unknown_user(self):
        self.client.post(
            reverse("web:groups_admin"),
            {
                "action": "add_member",
                "group_id": str(self.group.pk),
                "user_id": "00000000-0000-0000-0000-000000000000",
            },
        )
        self.assertEqual(GroupMembership.objects.filter(group=self.group).count(), 0)

    def test_add_member_still_accepts_a_typed_username(self):
        self.client.post(
            reverse("web:groups_admin"),
            {
                "action": "add_member",
                "group_id": str(self.group.pk),
                "username": "bob",
            },
        )
        self.assertTrue(
            GroupMembership.objects.filter(user=self.bob, group=self.group).exists()
        )


class CardCountRegressionTests(TestCase):
    """The card counters referenced model fields that do not exist.

    `{{ workspace.project_count }}` and `{{ project.document_count }}` rendered
    as nothing, so cards read "projects ·  documents".
    """

    def setUp(self):
        self.alice = User.objects.create_user("alice", "alice@example.com", "pw")
        self.workspace = WorkspaceService.create(name="Company", created_by=self.alice)
        self.project = ProjectService.create(
            workspace=self.workspace, name="Alpha", created_by=self.alice
        )
        self.client.force_login(self.alice)

    def test_dashboard_annotates_workspace_counts(self):
        workspace = self.client.get(reverse("web:dashboard")).context["workspaces"][0]
        self.assertEqual(workspace.project_count, 1)
        self.assertEqual(workspace.document_count, 0)

    def test_workspace_page_annotates_project_counts(self):
        response = self.client.get(reverse("web:workspace_detail", args=[self.workspace.slug]))
        self.assertEqual(response.context["projects"][0].document_count, 0)

    def test_counts_reach_the_rendered_card(self):
        html = self.client.get(reverse("web:dashboard")).content.decode()
        self.assertIn("1 projects", html)
        self.assertIn("0 documents", html)
        # A blank counter rendered as "projects ·  documents" with no number.
        self.assertNotIn("projects &middot;  documents", html)


@override_settings(KNOWLEDGE_DATA_ROOT=tempfile.mkdtemp())
class DocumentCountRegressionTests(TestCase):
    def setUp(self):
        self.alice = User.objects.create_user("alice", "alice@example.com", "pw")
        self.workspace = WorkspaceService.create(name="Company", created_by=self.alice)
        self.project = ProjectService.create(
            workspace=self.workspace, name="Alpha", created_by=self.alice
        )
        self.client.force_login(self.alice)

    def test_workspace_card_counts_project_documents(self):
        DocumentService.create(
            workspace=self.workspace, project=self.project, title="One", path="one.md"
        )
        DocumentService.create(
            workspace=self.workspace, project=self.project, title="Two", path="two.md"
        )
        workspace = self.client.get(reverse("web:dashboard")).context["workspaces"][0]
        self.assertEqual(workspace.document_count, 2)

    def test_project_card_counts_its_own_documents(self):
        DocumentService.create(
            workspace=self.workspace, project=self.project, title="One", path="one.md"
        )
        DocumentService.create(
            workspace=self.workspace, title="Loose", path="loose.md"
        )
        # The card lives on the workspace page and reads project.document_count.
        response = self.client.get(reverse("web:workspace_detail", args=[self.workspace.slug]))
        self.assertEqual(response.context["projects"][0].document_count, 1)
        html = response.content.decode()
        self.assertIn("1 documents", html)
        self.assertNotIn("> documents", html)


class CalendarParamRegressionTests(TestCase):
    """Hand-edited query params used to raise out of the view and return a 500."""

    def setUp(self):
        self.alice = User.objects.create_user("alice", "alice@example.com", "pw")
        self.client.force_login(self.alice)

    def test_calendar_survives_garbage_params(self):
        for query in ("year=abc", "month=abc", "month=0", "month=99", "year="):
            with self.subTest(query=query):
                response = self.client.get(f"{reverse('web:calendar')}?{query}")
                self.assertEqual(response.status_code, 200)

    def test_agenda_survives_garbage_params(self):
        for query in ("days=abc", "days=-5", "days=99999999", "days="):
            with self.subTest(query=query):
                response = self.client.get(f"{reverse('web:agenda')}?{query}")
                self.assertEqual(response.status_code, 200)

    def test_agenda_horizon_stays_sane(self):
        response = self.client.get(f"{reverse('web:agenda')}?days=99999999")
        self.assertLessEqual(response.context["days"], 365)

    def test_ical_feed_survives_garbage_params(self):
        for query in ("days=abc", "days=-5", "days=99999999"):
            with self.subTest(query=query):
                response = self.client.get(f"{reverse('web:deadlines_ical')}?{query}")
                self.assertEqual(response.status_code, 200)

    def test_every_calendar_week_row_has_seven_days(self):
        """Grid contract: no ragged last row, whatever the month starts on."""
        for year, month in ((2026, 9), (2026, 2), (2027, 3), (2027, 11)):
            with self.subTest(year=year, month=month):
                response = self.client.get(
                    f"{reverse('web:calendar')}?year={year}&month={month}"
                )
                weeks = calmod.Calendar(firstweekday=0).monthdayscalendar(year, month)
                self.assertTrue(all(len(week) == 7 for week in response.context["month_grid"]))
                html = response.content.decode()
                self.assertEqual(
                    html.count('<td class="cal-day'), 7 * len(weeks), f"{year}-{month}"
                )

    def test_calendar_navigation_uses_month_names(self):
        html = self.client.get(
            f"{reverse('web:calendar')}?year=2026&month=9"
        ).content.decode()
        self.assertIn("augusztus", html)
        self.assertIn("október", html)


class SearchModeRegressionTests(TestCase):
    def setUp(self):
        self.alice = User.objects.create_user("alice", "alice@example.com", "pw")
        self.client.force_login(self.alice)

    def test_unknown_mode_falls_back_to_hybrid(self):
        response = self.client.get(f"{reverse('web:search')}?mode=bogus&q=test")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["mode"], "hybrid")
