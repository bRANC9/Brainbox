from django.contrib import admin

from .models import GitCommitReference, GitRepository, GitSyncState


class GitSyncStateInline(admin.StackedInline):
    model = GitSyncState
    extra = 0
    can_delete = False
    readonly_fields = ("branch", "last_synced_sha", "last_pulled_at", "last_pushed_at", "last_error")


@admin.register(GitRepository)
class GitRepositoryAdmin(admin.ModelAdmin):
    list_display = ("name", "workspace", "project", "default_branch", "workflow", "is_active")
    list_filter = ("workflow", "is_active")
    search_fields = ("name", "remote_url")
    readonly_fields = ("resource", "created_at", "updated_at")
    inlines = [GitSyncStateInline]


@admin.register(GitCommitReference)
class GitCommitReferenceAdmin(admin.ModelAdmin):
    list_display = ("repository", "sha", "direction", "branch", "message", "created_at")
    list_filter = ("direction", "repository")
    search_fields = ("sha", "message")
    readonly_fields = [field.name for field in GitCommitReference._meta.fields]

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False
