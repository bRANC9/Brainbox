"""Django admin for the Resource tree, and the mixins the other apps build on.

The admin is a **lens, not an editor**. It is registered so a staff user can see
what exists and why they can or cannot see it; it deliberately has no write path
and no unfiltered list. Two rules do that work, both defined here so the other
apps cannot forget them:

* :class:`ReadOnlyAdminMixin` - no add, no change, no delete.
* :class:`ResourceScopedAdminMixin` - the changelist is cut down to the resources
  ``request.user`` may read.

An empty changelist for a user with no grants is the correct answer, not a bug.
A 404 on the model itself would be worse: an operator needs to be able to tell
"this exists and you may not see it" from "this does not exist", and the first
one is the only actionable one.

Nothing is unregistered here. These mixins only narrow and lock, so ``/admin/``
stays a usable diagnostic surface on a live instance.
"""

from django.contrib import admin
from django.db.models import Q
from django.urls import NoReverseMatch, reverse
from django.utils.html import format_html

from apps.permissions.constants import Permission
from apps.permissions.services import PermissionService

from .models import Resource


class ReadOnlyAdminMixin:
    """Refuse every write, on purpose.

    WHY THIS IS NOT JUST "be careful"
    ---------------------------------
    Every mutation in this codebase goes through a service, because the service
    is the only place that knows the full set of things that have to move
    together:

    * :class:`~apps.documents.services.DocumentService` writes the file on disk,
      bumps ``current_version``, appends a ``DocumentVersion``, de/re-indexes the
      chunk store, rebuilds the resource links and re-extracts the deadlines.
    * :class:`~apps.workspaces.services.WorkspaceService` /
      :class:`~apps.workspaces.services.ProjectService` create the ``Resource``
      and issue the owner's ADMIN grant inside one transaction, so a row can
      never come into existence without an owner and the ACL entry that matches.
    * :class:`~apps.files.services.FileService` and
      :class:`~apps.git.services.GitService` do the same for their own subtrees,
      and :class:`~apps.permissions.services.PermissionService` validates every
      ACL write against the caller's own reach.

    A ``ModelAdmin`` writes one column and stops. A hand-edited document would
    leave a stale file on disk, a version counter that disagrees with the history
    and an embedding index pointing at text that no longer exists. Worse, for a
    resource-backed row it lets somebody move ``owner`` or ``no_takeover``
    without touching the ACL, and those two are exactly what
    :class:`PermissionService` trusts to decide who is in and who is out.

    THE ONE ALLOWED WRITE PATH: the application service, reached from the web UI
    or the REST API. Not from here.
    """

    def has_add_permission(self, request, obj=None):
        # `resource` is a OneToOneField(primary_key=True) on most of these models,
        # so an admin-inserted row would have no id at all -- and, worse, no
        # owner. The service creates both in one transaction.
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


class ReadOnlyInlineMixin:
    """Inline counterpart of :class:`ReadOnlyAdminMixin`.

    A parent that is read-only must not offer "add another" or a delete button
    on its inlines: those post through the same form and would write without the
    parent service ever seeing it.
    """

    extra = 0
    can_delete = False

    def has_add_permission(self, request, obj=None):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


class ResourceScopedAdminMixin(ReadOnlyAdminMixin):
    """List only the rows whose Resource the logged-in user may read.

    ``resource_id_fields`` names the local FK column(s) that reach a
    ``Resource``. A single column is the common case (the field *is* the primary
    key on Workspace/Project/Document/File). Dotted paths work for rows that
    reach it through a parent (``"document__resource_id"`` on a version row), and
    several columns are OR-ed - a :class:`~apps.links.models.ResourceLink` is
    listed when either end is readable.

    The filter is the same :meth:`PermissionService.allowed_resource_ids` call
    the REST and MCP surfaces use, so a resource that is hidden in the API is
    hidden here too, including for a superuser: a superuser has no implicit
    content access, only an audited takeover.
    """

    resource_id_fields: tuple[str, ...] = ("resource_id",)

    def get_queryset(self, request):
        queryset = super().get_queryset(request)
        fields = self.resource_id_fields
        candidates: set = set()
        for field in fields:
            candidates.update(queryset.values_list(field, flat=True))
        allowed = set(
            PermissionService.allowed_resource_ids(
                request.user, candidates, Permission.READ
            )
        )
        if not allowed:
            return queryset.none()
        query = Q()
        for field in fields:
            query |= Q(**{f"{field}__in": allowed})
        return queryset.filter(query)


class ResourceBackedAdminMixin(ResourceScopedAdminMixin):
    """Shared behaviour for models whose ``resource`` is their primary key.

    Two things follow from that shape, and both used to bite:

    * **Creating is impossible in the admin.** ``resource`` is a
      ``OneToOneField(primary_key=True)``, so it is also the row id. It is
      correctly listed in ``readonly_fields`` (there is no sensible value to
      type), which leaves the add form with no way to supply the id -- the POST
      dies on ``null value in column "resource_id"``. These rows must come from
      the application service, which creates the Resource, sets ``created_by``
      and issues the owner's ADMIN grant in one transaction. Letting the admin
      do it would produce exactly the ownerless object that service exists to
      prevent, so add is disabled instead.

    * **Rendering an unsaved instance raised.** ``permission_link`` reverse()d
      with ``obj.resource_id``, which is ``None`` before save, and the URL
      pattern requires a UUID -- so even *viewing* the add form 500'd with
      ``NoReverseMatch``.
    """

    def permission_link(self, obj):
        resource_id = getattr(obj, "resource_id", None)
        if not resource_id:
            return "—"
        try:
            url = reverse("web:resource_permissions", args=[resource_id])
        except NoReverseMatch:  # pragma: no cover - URLconf drift
            return "—"
        return format_html('<a href="{}" target="_blank">Manage permissions →</a>', url)

    permission_link.short_description = "Access control"


@admin.register(Resource)
class ResourceAdmin(ReadOnlyAdminMixin, admin.ModelAdmin):
    """The identity table itself, so the ACL chain can be read end to end.

    Filtered with the same call as everything else; ``Resource`` is the thing the
    permission engine evaluates, so there is no ``resource_id`` column to filter
    on - the row *is* the resource. ``no_takeover`` is readonly because flipping
    it is the one switch that decides whether an audited superuser takeover can
    ever reach this subtree, and only the workspace owner may flip it (via
    :class:`~apps.workspaces.ownership.OwnershipService`).
    """

    list_display = (
        "name",
        "resource_type",
        "parent",
        "no_takeover",
        "created_by",
        "created_at",
    )
    list_filter = ("resource_type", "no_takeover")
    search_fields = ("name",)
    autocomplete_fields = ("parent",)
    readonly_fields = ("no_takeover", "permission_link", "metadata", "created_at", "updated_at")

    def permission_link(self, obj):
        try:
            url = reverse("web:resource_permissions", args=[obj.pk])
        except NoReverseMatch:  # pragma: no cover - URLconf drift
            return "—"
        return format_html('<a href="{}" target="_blank">Manage permissions →</a>', url)

    permission_link.short_description = "Access control"

    def get_queryset(self, request):
        queryset = super().get_queryset(request)
        ids = list(queryset.values_list("id", flat=True))
        return queryset.filter(
            id__in=PermissionService.allowed_resource_ids(
                request.user, ids, Permission.READ
            )
        )
