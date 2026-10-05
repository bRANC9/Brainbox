from django.contrib import admin

from apps.resources.admin import ResourceScopedAdminMixin

from .models import GatewayTarget


@admin.register(GatewayTarget)
class GatewayTargetAdmin(ResourceScopedAdminMixin, admin.ModelAdmin):
    resource_id_fields = ("resource_id",)
    list_display = ("name", "kind", "base_url", "enabled", "owner", "workspace", "project")
    list_filter = ("kind", "enabled")
    search_fields = ("name", "base_url")
    readonly_fields = ("resource", "created_at", "updated_at")
