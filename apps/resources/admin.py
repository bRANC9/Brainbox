from django.contrib import admin

from .models import Resource


@admin.register(Resource)
class ResourceAdmin(admin.ModelAdmin):
    list_display = ("name", "resource_type", "parent", "created_at")
    list_filter = ("resource_type",)
    search_fields = ("name",)
    autocomplete_fields = ("parent",)
