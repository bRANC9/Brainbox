from django.contrib import admin

from apps.resources.admin import ResourceScopedAdminMixin

from .models import Fact, MemoryAggregate


@admin.register(Fact)
class FactAdmin(ResourceScopedAdminMixin, admin.ModelAdmin):
    resource_id_fields = ("source_resource_id",)
    list_display = ("subject", "predicate", "object", "origin", "workspace", "extracted_at")
    list_filter = ("predicate", "origin")
    search_fields = ("subject", "object")
    readonly_fields = [field.name for field in Fact._meta.fields]


@admin.register(MemoryAggregate)
class MemoryAggregateAdmin(admin.ModelAdmin):
    list_display = ("subject", "origin", "workspace", "created_at")
    list_filter = ("origin",)
    search_fields = ("subject", "text")
    readonly_fields = ("created_at", "updated_at")

