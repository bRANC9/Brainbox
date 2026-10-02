"""Read-only admin for git repositories, credentials and commit references.

Write path: :class:`~apps.git.services.GitService` (create/configure/sync) and
the sync loop itself. ``last_error`` and the commit trailers are produced by the
worker; a hand-edited repository row would desynchronise the database from the
checkout that is the actual source of truth.
"""

from django.contrib import admin

from apps.resources.admin import (
    ReadOnlyInlineMixin,
    ResourceBackedAdminMixin,
    ResourceScopedAdminMixin,
)

from .models import GitCommitReference, GitCredential, GitRepository, GitSyncState


class GitSyncStateInline(ReadOnlyInlineMixin, admin.StackedInline):
    model = GitSyncState
    readonly_fields = ("branch", "last_synced_sha", "last_pulled_at", "last_pushed_at", "last_error")


class GitCredentialInline(ReadOnlyInlineMixin, admin.TabularInline):
    model = GitCredential
    autocomplete_fields = ("user", "secret")


@admin.register(GitRepository)
class GitRepositoryAdmin(ResourceBackedAdminMixin, admin.ModelAdmin):
    list_display = ("name", "workspace", "project", "secret", "default_branch", "workflow", "is_active")
    list_filter = ("workflow", "is_active")
    search_fields = ("name", "remote_url")
    readonly_fields = (
        "resource",
        "permission_link",
        "created_by",
        "created_at",
        "updated_at",
    )
    inlines = [GitSyncStateInline, GitCredentialInline]


@admin.register(GitCredential)
class GitCredentialAdmin(ResourceScopedAdminMixin, admin.ModelAdmin):
    # A credential points at somebody's Secret, so it is scoped to the
    # repository the caller may read rather than to the credential's owner.
    resource_id_fields = ("repository__resource_id",)
    list_display = ("user", "repository", "secret", "created_at")
    list_filter = ("repository",)
    search_fields = ("user__username", "repository__name", "secret__name")
    autocomplete_fields = ("user", "secret")
    readonly_fields = [field.name for field in GitCredential._meta.fields]


@admin.register(GitCommitReference)
class GitCommitReferenceAdmin(ResourceScopedAdminMixin, admin.ModelAdmin):
    resource_id_fields = ("repository__resource_id",)
    list_display = ("repository", "sha", "direction", "branch", "message", "created_at")
    list_filter = ("direction", "repository")
    search_fields = ("sha", "message")
    readonly_fields = [field.name for field in GitCommitReference._meta.fields]
