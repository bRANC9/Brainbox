from django.contrib import admin

from .models import AuditEvent


@admin.register(AuditEvent)
class AuditEventAdmin(admin.ModelAdmin):
    list_display = ("timestamp", "action", "result", "user", "api_key", "source", "resource")
    list_filter = ("action", "result", "source")
    search_fields = ("user__username", "resource__name", "detail")
    date_hierarchy = "timestamp"
    readonly_fields = [f.name for f in AuditEvent._meta.fields]

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False
