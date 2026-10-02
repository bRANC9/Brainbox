from django.contrib import admin, messages
from django.contrib.auth.admin import UserAdmin as DjangoUserAdmin

from apps.accounts.services import OwnerConflict, UserService

from .models import ApiKey, ApiKeyScope, User


@admin.register(User)
class UserAdmin(DjangoUserAdmin):
    """The user table, with the ownership rules the REST API enforces.

    Deleting a user is refused while they own shared content, because
    ``Workspace.owner``/``Project.owner`` are PROTECT: a 300-document workspace
    must not go down with one row. Hiding the button and saying *why* beats a 500.
    Offboarding is normally ``is_active = False`` - use the "Eltávolítás" action.
    """

    list_display = ("username", "email", "display_name", "is_staff", "is_active")
    list_filter = ("is_active", "is_staff", "is_superuser")
    search_fields = ("username", "email", "display_name")
    fieldsets = DjangoUserAdmin.fieldsets + (
        ("Brainbox", {"fields": ("display_name",)}),
    )
    actions = ("deactivate_selected",)

    @admin.display(description="tulajdon")
    def owned_count(self, obj):
        workspaces, projects = UserService.owned_by(obj)
        return f"{len(workspaces)} ws / {len(projects)} pr"

    def get_extra_context(self, obj):
        extra = super().get_extra_context(obj)
        if obj is not None and obj.pk:
            extra["bb_owned"] = self.owned_count(obj)
        return extra

    def has_delete_permission(self, request, obj=None):
        if obj is None:
            return True
        return UserService.can_delete(obj)

    def delete_model(self, request, obj):
        try:
            UserService.delete(user=obj, actor=request.user, request=request)
        except OwnerConflict as exc:
            # has_delete_permission already gates this; belt and braces so a
            # race (ownership transferred in another tab) cannot 500.
            self.message_user(
                request, exc.detail["hint"], level=messages.ERROR
            )
            return
        super().delete_model(request, obj)

    def delete_queryset(self, request, queryset):
        deletable, blocked = [], []
        for candidate in queryset:
            (deletable if UserService.can_delete(candidate) else blocked).append(candidate)
        for candidate in blocked:
            self.message_user(
                request,
                f"{candidate.username}: megosztott tartalmat birtokol "
                f"({self.owned_count(candidate)}) - kihagyva. "
                "Adj át a tartalmat, vagy deaktiváld.",
                level=messages.WARNING,
            )
        if deletable:
            super().delete_queryset(request, User.objects.filter(pk__in=[u.pk for u in deletable]))

    @admin.action(description="Eltávolítás (deaktiválás, adatok maradnak)")
    def deactivate_selected(self, request, queryset):
        """Offboard without data loss - the usual answer, not deletion."""
        for candidate in queryset:
            if candidate.pk == request.user.pk:
                self.message_user(
                    request, "Magadat nem tudod deaktiválni.", level=messages.ERROR
                )
                continue
            UserService.deactivate(user=candidate, actor=request.user, request=request)
        self.message_user(
            request, f"{queryset.count()} felhasználó deaktiválva.", level=messages.SUCCESS
        )


class ApiKeyScopeInline(admin.TabularInline):
    model = ApiKeyScope
    extra = 0


@admin.register(ApiKey)
class ApiKeyAdmin(admin.ModelAdmin):
    list_display = ("name", "user", "key_prefix", "is_active", "created_at", "last_used_at")
    list_filter = ("is_active",)
    search_fields = ("name", "key_prefix", "user__username")
    readonly_fields = ("key_prefix", "key_hash", "created_at", "last_used_at", "revoked_at")
    inlines = [ApiKeyScopeInline]
