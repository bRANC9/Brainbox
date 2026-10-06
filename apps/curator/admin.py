from django.contrib import admin

from apps.resources.admin import ResourceScopedAdminMixin

from .models import CuratorProposal


@admin.register(CuratorProposal)
class CuratorProposalAdmin(ResourceScopedAdminMixin, admin.ModelAdmin):
    resource_id_fields = ("resource_id",)
    list_display = ("title", "kind", "status", "workspace", "created_at", "decided_at")
    list_filter = ("kind", "status")
    search_fields = ("title", "signature")
    readonly_fields = ("created_at", "updated_at")
