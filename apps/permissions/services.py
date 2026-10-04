"""Central permission engine.

Every REST/MCP/web entry point resolves access through this service. ACLs live
on Resources; a domain object's resource is reached via ``obj.resource``.

Resolution walks the whole inheritance chain (resource -> parent -> ... ->
workspace) and applies two rules:

* a DENY at *any* level is absolute and stops the walk. A narrow ALLOW deeper in
  the tree never re-opens what a broader DENY closed.
* otherwise the first ALLOW that implies the required permission wins.

If nothing matches, access is denied. An API key can only narrow, never widen,
its owner's permissions.

Two further invariants are enforced here rather than in the callers, because the
callers are three different surfaces (web UI, REST, MCP) and three copies of a
rule is three ways to forget it:

* :meth:`can_manage_acl` - who may change the ACL of a resource at all.
* :meth:`grant` - validates permission level *and* subject before writing, and
  raises :class:`PermissionDenied`. The unchecked variant is
  :meth:`grant_unchecked` and exists only for owner bootstrap and migrations.

A superuser has **no** implicit content access. The only way in is the explicit,
audited takeover (``can_take_over``), which writes a normal ACL entry. This is
controlled by ``BRAINBOX_SUPERUSER_BYPASS`` (default off) which restores the old
behaviour during the rollout; see ``manage.py access_audit``.
"""

from __future__ import annotations

from django.conf import settings
from django.core.exceptions import PermissionDenied
from django.db.models import Q

from apps.groups.models import Group, GroupMembership
from apps.resources.models import Resource, ResourceType

from .constants import Effect, Permission, SubjectType, allows, deny_blocks
from .models import ResourceACL

#: How deep the descendant walk goes when building the sharing audience. A
#: workspace -> project -> folder -> ... chain is far shallower than this; the
#: bound only exists so a corrupted parent cycle cannot spin forever.
MAX_HIERARCHY_DEPTH = 16


class PermissionService:
    # -- public API ----------------------------------------------------------
    @classmethod
    def check(cls, user, resource, permission: str, api_key=None) -> bool:
        if user is None or not getattr(user, "is_authenticated", False):
            return False
        if resource is None:
            return False
        if not cls._user_check(user, resource, permission):
            return False
        if api_key is not None and not cls._api_key_check(api_key, resource, permission):
            return False
        return True

    @classmethod
    def allowed_resource_ids(cls, user, resource_ids, permission: str, api_key=None) -> list:
        ids = list(resource_ids)
        if not ids:
            return []
        if user is None or not getattr(user, "is_authenticated", False):
            return []
        if cls.superuser_bypass() and getattr(user, "is_superuser", False) and api_key is None:
            return ids
        resources = Resource.objects.filter(id__in=ids).select_related("parent")
        return [r.id for r in resources if cls.check(user, r, permission, api_key)]

    @classmethod
    def visible_resource_ids(
        cls, user, resource_ids, permission: str = Permission.READ, api_key=None
    ) -> list:
        """Readable ids *plus* the ancestors needed to reach them.

        A folder tree is a path, so a caller granted access to ``A/B/C`` has to
        see ``A`` and ``A/B`` to be able to navigate to it. Ancestors are
        structural only: their other children stay hidden, and content access
        is still decided by :meth:`check` alone.
        """
        ids = list(resource_ids)
        allowed = set(cls.allowed_resource_ids(user, ids, permission, api_key=api_key))
        if not allowed:
            return []
        by_id = {
            r.id: r
            for r in Resource.objects.filter(id__in=ids).select_related("parent")
        }
        for resource_id in list(allowed):
            node = by_id.get(resource_id)
            if node is None:
                continue
            for ancestor in node.ancestors():
                if ancestor.id in by_id:
                    allowed.add(ancestor.id)
        return [rid for rid in ids if rid in allowed]

    @classmethod
    def can_browse(cls, user, resource, api_key=None) -> bool:
        """Whether the caller may open this container's page at all.

        Reading is not enough to *reach* a grant. If somebody was given
        ``Ecoform/Fejlesztoi resz/runbooks/2024`` and nothing above it, they can
        open that document and find it in search - but a plain READ check on the
        project 404s, so the tree that would show them the way in is never
        rendered. The trail closure that makes the folder navigable
        (:meth:`visible_resource_ids`) has to apply to the container page too,
        otherwise "grant me a deep folder" silently does not work.

        Cheap by construction: the caller either already has READ on the
        container, or there is an ALLOW entry of theirs (or of one of their
        groups) on some resource below it. No subtree walk needed, because any
        ancestor ALLOW would already have satisfied the first check.
        """
        if resource is None:
            return False
        if user is None or not getattr(user, "is_authenticated", False):
            return False
        if resource.resource_type == ResourceType.FOLDER:
            return False
        # Absolute DENY wins first. The subtree shortcut below is only a way of
        # *reaching* a grant that already exists further down; it must never be a
        # way around a DENY sitting on the container itself, or a denied
        # project would be opened by the very helper meant to make grants
        # navigable.
        if cls._denied_on_chain(user, resource, Permission.READ, api_key):
            return False
        if cls.check(user, resource, Permission.READ, api_key=api_key):
            return True
        subtree = cls._descendant_resource_ids(resource)
        subtree.discard(resource.id)
        if not subtree:
            return False
        query = Q(subject_type=SubjectType.USER, subject_id=user.id)
        group_ids = cls._group_ids(user)
        if group_ids:
            query |= Q(subject_type=SubjectType.GROUP, subject_id__in=group_ids)
        return (
            ResourceACL.objects.filter(resource_id__in=subtree)
            .filter(effect=Effect.ALLOW)
            .filter(query)
            .exists()
        )

    @classmethod
    def _denied_on_chain(cls, user, resource, permission: str, api_key=None) -> bool:
        """True when a DENY on this resource or an ancestor blocks ``permission``."""
        group_ids = cls._group_ids(user)
        for index, node in enumerate(resource.ancestors()):
            entries = cls._entries_for(node, user, group_ids)
            if index > 0:
                entries = [entry for entry in entries if entry.inherit]
            if any(
                entry.effect == Effect.DENY and deny_blocks(entry.permission, permission)
                for entry in entries
            ):
                return True
        return False

    @classmethod
    def superuser_bypass(cls) -> bool:
        """Temporary rollout escape hatch; off unless explicitly enabled."""
        return bool(getattr(settings, "BRAINBOX_SUPERUSER_BYPASS", False))

    # -- user / group resolution --------------------------------------------
    @classmethod
    def _user_check(cls, user, resource: Resource, permission: str) -> bool:
        if cls.superuser_bypass() and getattr(user, "is_superuser", False):
            return True
        group_ids = cls._group_ids(user)
        allowed = False
        for index, node in enumerate(resource.ancestors()):
            entries = cls._entries_for(node, user, group_ids)
            if index > 0:
                entries = [entry for entry in entries if entry.inherit]
            if not entries:
                continue
            if any(
                entry.effect == Effect.DENY and deny_blocks(entry.permission, permission)
                for entry in entries
            ):
                # Absolute: a deny anywhere on the chain wins, no matter how
                # narrow an ALLOW sits below it.
                return False
            if not allowed and any(
                entry.effect == Effect.ALLOW and allows(entry.permission, permission)
                for entry in entries
            ):
                allowed = True
        return allowed

    @classmethod
    def _entries_for(cls, resource: Resource, user, group_ids) -> list[ResourceACL]:
        query = Q(subject_type=SubjectType.USER, subject_id=user.id)
        if group_ids:
            query |= Q(subject_type=SubjectType.GROUP, subject_id__in=group_ids)
        return list(ResourceACL.objects.filter(resource=resource).filter(query))

    @classmethod
    def _group_ids(cls, user) -> list:
        return list(
            GroupMembership.objects.filter(user=user).values_list("group_id", flat=True)
        )

    # -- api key narrowing ---------------------------------------------------
    @classmethod
    def _api_key_check(cls, api_key, resource: Resource, permission: str) -> bool:
        scopes = list(api_key.scopes.all())
        if not scopes:
            return True
        workspace = resource.workspace_resource()
        project = resource.project_resource()
        workspace_id = workspace.id if workspace else None
        project_id = project.id if project else None
        applicable = [
            scope for scope in scopes if scope.applies_to(workspace_id, project_id)
        ]
        if not applicable:
            return False
        if any(
            scope.effect == Effect.DENY and deny_blocks(scope.permission, permission)
            for scope in applicable
        ):
            return False
        if any(
            scope.effect == Effect.ALLOW and allows(scope.permission, permission)
            for scope in applicable
        ):
            return True
        return False

    # -- ownership -----------------------------------------------------------
    @classmethod
    def workspace_of(cls, resource: Resource):
        workspace_resource = resource.workspace_resource()
        if workspace_resource is None:
            return None
        return getattr(workspace_resource, "workspace", None)

    @classmethod
    def project_of(cls, resource: Resource):
        project_resource = resource.project_resource()
        if project_resource is None:
            return None
        return getattr(project_resource, "project", None)

    @classmethod
    def scope_owner_id(cls, resource: Resource):
        """The user who owns the sharing scope this resource lives in.

        A project owner outranks the workspace owner inside that project, so
        locking down your own project does not need the workspace owner.
        """
        if resource.resource_type == ResourceType.WORKSPACE:
            workspace = getattr(resource, "workspace", None)
            return workspace.owner_id if workspace else None
        project = cls.project_of(resource)
        if project is not None and project.owner_id:
            return project.owner_id
        workspace = cls.workspace_of(resource)
        return workspace.owner_id if workspace else None

    @classmethod
    def is_scope_owner(cls, user, resource: Resource) -> bool:
        owner_id = cls.scope_owner_id(resource)
        return bool(
            owner_id
            and user is not None
            and getattr(user, "is_authenticated", False)
            and str(owner_id) == str(user.id)
        )

    @classmethod
    def is_personal(cls, resource: Resource) -> bool:
        workspace = cls.workspace_of(resource)
        return bool(workspace is not None and workspace.is_personal)

    @classmethod
    def _has_direct_admin_entry(cls, user, resource: Resource) -> bool:
        """An ADMIN entry written on this exact resource (index 0, not inherited).

        This is what an audited superuser takeover leaves behind, and it is the
        only thing that lets someone other than the owner manage a workspace ACL.
        """
        return ResourceACL.objects.filter(
            resource=resource,
            subject_type=SubjectType.USER,
            subject_id=user.id,
            permission=Permission.ADMIN,
            effect=Effect.ALLOW,
        ).exists()

    # -- who may hand out access --------------------------------------------
    @classmethod
    def can_manage_acl(cls, user, resource, api_key=None) -> bool:
        """Whether the caller may create/remove ACL entries on this resource.

        On a *workspace* only the owner answers this - "who is allowed to be in
        this workspace" is the owner's decision and nobody else's. Everywhere
        inside, a normal ADMIN does.
        """
        if resource is None:
            return False
        if user is None or not getattr(user, "is_authenticated", False):
            return False
        if cls.is_scope_owner(user, resource):
            return True
        if resource.resource_type == ResourceType.WORKSPACE:
            return cls._has_direct_admin_entry(user, resource)
        return cls.check(user, resource, Permission.ADMIN, api_key=api_key)

    @classmethod
    def max_grantable(cls, user, resource) -> str | None:
        """The strongest permission the caller may grant on this resource."""
        if cls.can_manage_acl(user, resource):
            return Permission.ADMIN
        if PermissionService.check(user, resource, Permission.WRITE):
            return Permission.WRITE
        if PermissionService.check(user, resource, Permission.READ):
            return Permission.READ
        return None

    @classmethod
    def can_grant(
        cls, user, resource, permission: str, effect: str = Effect.ALLOW, api_key=None
    ) -> bool:
        """Whether the caller may create this ACL entry.

        A DENY is an owner-only instrument: it *takes access away*, so only the
        user who owns the sharing scope may use one - not even a delegated admin,
        and not even a superuser acting on an audited takeover (which is a way
        in, not a way to lock the owner out). An ADMIN may grant anything; a
        writer may *share* the resource but never escalate.
        """
        if resource is None:
            return False
        if effect == Effect.DENY:
            return cls.is_scope_owner(user, resource)
        if cls.can_manage_acl(user, resource, api_key=api_key):
            return True
        return permission == Permission.READ and cls._shareable_only(
            user, resource, api_key=api_key
        )

    @classmethod
    def _shareable_only(cls, user, resource, api_key=None) -> bool:
        """A writer may share only *inside* a workspace they can read.

        Resources without a workspace ancestor (secrets, MCP servers, a root
        git repository) are outside every workspace and are never shareable
        through this path.
        """
        if resource.workspace_resource() is None:
            return False
        return PermissionService.check(user, resource, Permission.WRITE, api_key=api_key)

    # -- who may be shared with ---------------------------------------------
    @classmethod
    def grantable_subjects(cls, user, resource, api_key=None) -> tuple[set, set]:
        """The (user ids, group ids) the caller may write an ACL entry for.

        The workspace/project owner sees the full directory: they define the
        audience and this is the only way a brand new person can be brought in.
        A delegated admin sees only the audience that already exists inside the
        workspace plus their own work group, so they can neither widen the circle
        beyond what the owner granted them nor enumerate the company directory.
        """
        users: set = set()
        groups: set = set()
        if resource is None or user is None or not getattr(user, "is_authenticated", False):
            return users, groups
        if cls.is_personal(resource):
            owner_id = cls.scope_owner_id(resource)
            if owner_id and str(owner_id) == str(user.id):
                users.add(owner_id)
            return users, groups
        if cls.is_scope_owner(user, resource):
            from django.contrib.auth import get_user_model

            users = set(
                get_user_model()
                .objects.filter(is_active=True)
                .values_list("id", flat=True)
            )
            return users, set(Group.objects.values_list("id", flat=True))
        if not cls.check(user, resource, Permission.WRITE, api_key=api_key):
            return users, groups
        for subject_type, subject_id in cls._workspace_audience(resource):
            if subject_type == SubjectType.USER:
                users.add(subject_id)
            elif subject_type == SubjectType.GROUP:
                groups.add(subject_id)
        own_group_ids = set(cls._group_ids(user))
        groups |= own_group_ids
        users |= set(
            GroupMembership.objects.filter(group_id__in=own_group_ids).values_list(
                "user_id", flat=True
            )
        )
        users.add(user.id)
        owner_id = cls.scope_owner_id(resource)
        if owner_id:
            users.add(owner_id)
        return users, groups

    @classmethod
    def can_grant_to(
        cls, user, resource, subject_type: str, subject_id, api_key=None
    ) -> bool:
        if resource is None:
            return False
        if not cls.check(user, resource, Permission.WRITE, api_key=api_key):
            return False
        if cls.is_personal(resource):
            return (
                subject_type == SubjectType.USER
                and str(subject_id) == str(cls.scope_owner_id(resource))
            )
        if cls.is_scope_owner(user, resource):
            return True
        users, groups = cls.grantable_subjects(user, resource, api_key=api_key)
        if subject_type == SubjectType.GROUP:
            return subject_id in groups
        return subject_id in users

    @classmethod
    def _workspace_audience(cls, resource: Resource) -> set:
        """Subject ids that already hold an ACL entry somewhere in the workspace."""
        workspace = resource.workspace_resource()
        if workspace is None:
            return set()
        return set(
            ResourceACL.objects.filter(
                resource_id__in=cls._descendant_resource_ids(workspace)
            )
            .values_list("subject_type", "subject_id")
            .distinct()
        )

    @classmethod
    def _descendant_resource_ids(cls, root: Resource) -> set:
        seen = {root.id}
        current = [root.id]
        for _ in range(MAX_HIERARCHY_DEPTH):
            children = list(
                Resource.objects.filter(parent_id__in=current).values_list("id", flat=True)
            )
            children = [child for child in children if child not in seen]
            if not children:
                break
            seen.update(children)
            current = children
        return seen

    # -- takeover ------------------------------------------------------------
    @classmethod
    def can_take_over(cls, user, resource) -> bool:
        """The explicit, audited way in for a superuser. Off by design everywhere else.

        Only offered for what the caller *cannot* reach: taking over something you
        already administer is meaningless, and offering it there put a
        full-width "jogosultság-átvétel" bar at the top of the owner's own pages.
        """
        if resource is None or user is None:
            return False
        if not getattr(user, "is_authenticated", False) or not user.is_superuser:
            return False
        if resource.takeover_locked():
            return False
        if cls.check(user, resource, Permission.ADMIN):
            return False
        return True

    # -- mutations -----------------------------------------------------------
    @classmethod
    def grant(
        cls,
        resource: Resource,
        *,
        subject_type: str,
        subject_id,
        permission: str,
        effect: str = Effect.ALLOW,
        inherit: bool = True,
        created_by=None,
        api_key=None,
    ) -> ResourceACL:
        """Validated write. Raises PermissionDenied when the caller may not.

        Every surface goes through this, so a caller cannot grant more than it
        holds, cannot share outside its own work group, and cannot touch a
        personal workspace. Use :meth:`grant_unchecked` only where the caller is
        the owner by construction (object creation) or is a migration.
        """
        if resource is None:
            raise PermissionDenied("Cannot grant access to a missing resource.")
        if not cls.can_grant(created_by, resource, permission, effect, api_key=api_key):
            raise PermissionDenied("You cannot grant that permission here.")
        if not cls.can_grant_to(
            created_by, resource, subject_type, subject_id, api_key=api_key
        ):
            raise PermissionDenied("That subject is not available for sharing here.")
        return cls.grant_unchecked(
            resource,
            subject_type=subject_type,
            subject_id=subject_id,
            permission=permission,
            effect=effect,
            inherit=inherit,
            created_by=created_by,
        )

    @classmethod
    def grant_unchecked(
        cls,
        resource: Resource,
        *,
        subject_type: str,
        subject_id,
        permission: str,
        effect: str = Effect.ALLOW,
        inherit: bool = True,
        created_by=None,
    ) -> ResourceACL:
        """Write an ACL entry without validating the caller.

        Reserved for owner bootstrap (the creator has no grant yet) and for data
        migrations. Anything else belongs in :meth:`grant`.
        """
        entry, _ = ResourceACL.objects.update_or_create(
            resource=resource,
            subject_type=subject_type,
            subject_id=subject_id,
            permission=permission,
            defaults={"effect": effect, "inherit": inherit, "created_by": created_by},
        )
        return entry

    @classmethod
    def revoke(
        cls, resource: Resource, *, subject_type: str, subject_id, permission=None, actor=None
    ):
        if not cls.can_manage_acl(actor, resource):
            raise PermissionDenied("You cannot change access on this resource.")
        queryset = ResourceACL.objects.filter(
            resource=resource, subject_type=subject_type, subject_id=subject_id
        )
        if permission:
            queryset = queryset.filter(permission=permission)
        return queryset.delete()


__all__ = ["PermissionService", "Permission", "PermissionDenied"]
