from django.contrib import admin

from .models import ResourceLink


@admin.register(ResourceLink)
class ResourceLinkAdmin(admin.ModelAdmin):
    list_display = ("source", "link_type", "target", "created_at")
    list_filter = ("link_type",)
    search_fields = ("source__name", "target__name")
    autocomplete_fields = ("source", "target")
