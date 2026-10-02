"""Read-only admin for the audit trail.

This one is deliberately **not** filtered down to the caller's readable
resources, unlike every other admin here. The audit log is the one table whose
value is that it is complete: an operator debugging "why can I not see X?" needs
the *denied* events for X, and those are exactly the rows a resource filter would
hide. Gaps in a security log are worse than a resource name in it, and the log
already carries no content - an action, a result, a resource id, a user.

Write path: :meth:`AuditService.log`, from the service layer. The model refuses
updates on its own, too.
"""

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
