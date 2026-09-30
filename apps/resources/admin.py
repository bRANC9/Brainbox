from django.contrib import admin
from django.urls import NoReverseMatch, reverse
from django.utils.html import format_html

from .models import Resource


class ResourceBackedAdminMixin:
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

    def has_add_permission(self, request, obj=None):
        return False

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
class ResourceAdmin(admin.ModelAdmin):
    list_display = ("name", "resource_type", "parent", "created_at")
    list_filter = ("resource_type",)
    search_fields = ("name",)
    autocomplete_fields = ("parent",)