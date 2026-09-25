from django.contrib import admin

from .models import ResourceACL


@admin.register(ResourceACL)
class ResourceACLAdmin(admin.ModelAdmin):
    list_display = (
        "resource",
        "subject_type",
        "subject_id",
        "permission",
        "effect",
        "inherit",
        "created_at",
    )
    list_filter = ("subject_type", "permission", "effect", "inherit")
    search_fields = ("resource__name", "subject_id")
    autocomplete_fields = ("resource",)
