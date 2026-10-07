from django.contrib import admin

from apps.resources.admin import ResourceScopedAdminMixin

from .models import Fact


@admin.register(Fact)
class FactAdmin(ResourceScopedAdminMixin, admin.ModelAdmin):
    resource_id_fields = ("source_resource_id",)
    list_display = ("subject", "predicate", "object", "workspace", "extracted_at")
    list_filter = ("predicate",)
    search_fields = ("subject", "object")
    readonly_fields = [field.name for field in Fact._meta.fields]
