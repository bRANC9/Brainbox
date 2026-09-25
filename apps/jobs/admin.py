from django.contrib import admin

from .models import Job, JobRun, SchedulerState


class JobRunInline(admin.TabularInline):
    model = JobRun
    extra = 0
    can_delete = False
    fields = ("run_id", "status", "trigger", "attempt", "created_at", "duration_ms", "error")
    readonly_fields = fields

    def has_add_permission(self, request, obj=None):
        return False


@admin.register(Job)
class JobAdmin(admin.ModelAdmin):
    list_display = ("name", "task_key", "schedule_kind", "schedule_description", "enabled", "next_run_at", "last_run_at")
    list_filter = ("enabled", "schedule_kind", "task_key")
    search_fields = ("name", "task_key")
    inlines = [JobRunInline]

    @admin.display(description="config errors")
    def config_errors(self, obj):
        errors = obj.config_errors
        return "; ".join(errors) if errors else "-"

    readonly_fields = ("next_run_at", "last_run_at", "config_errors")


@admin.register(JobRun)
class JobRunAdmin(admin.ModelAdmin):
    list_display = ("run_id", "task_key", "status", "trigger", "attempt", "created_at", "duration_ms", "worker")
    list_filter = ("status", "trigger", "task_key")
    search_fields = ("task_key", "run_id", "worker")
    readonly_fields = [f.name for f in JobRun._meta.fields]

    def has_add_permission(self, request):
        return False


@admin.register(SchedulerState)
class SchedulerStateAdmin(admin.ModelAdmin):
    list_display = ("name", "owner", "locked_until", "heartbeat_at", "boot_id")
    readonly_fields = [f.name for f in SchedulerState._meta.fields]

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False
