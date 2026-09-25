from django.contrib import admin

from .models import Document, DocumentVersion


class DocumentVersionInline(admin.TabularInline):
    model = DocumentVersion
    extra = 0
    can_delete = False
    fields = ("version", "change_type", "source", "created_by", "created_at")
    readonly_fields = fields

    def has_add_permission(self, request, obj=None):
        return False


@admin.register(Document)
class DocumentAdmin(admin.ModelAdmin):
    list_display = ("title", "workspace", "project", "status", "priority", "current_version", "updated_at")
    list_filter = ("status", "workspace")
    search_fields = ("title", "path", "summary")
    readonly_fields = ("resource", "current_version", "created_at", "updated_at")
    inlines = [DocumentVersionInline]


@admin.register(DocumentVersion)
class DocumentVersionAdmin(admin.ModelAdmin):
    list_display = ("document", "version", "change_type", "source", "created_by", "created_at")
    list_filter = ("change_type", "source")
    search_fields = ("document__title",)

    def has_add_permission(self, request):
        return False
