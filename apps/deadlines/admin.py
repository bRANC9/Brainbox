from django.contrib import admin

from .models import KnowledgeDeadline


@admin.register(KnowledgeDeadline)
class KnowledgeDeadlineAdmin(admin.ModelAdmin):
    list_display = ("due_date", "title", "document", "workspace", "project", "status", "source", "confidence")
    list_filter = ("status", "source", "workspace", "project")
    search_fields = ("title", "document__title", "context")
    date_hierarchy = "due_date"
    autocomplete_fields = ("document", "resource", "workspace", "project")
    readonly_fields = ("source_ref", "created_at", "updated_at")
