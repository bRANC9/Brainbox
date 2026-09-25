from django.contrib import admin
from django.contrib.auth.admin import UserAdmin as DjangoUserAdmin

from .models import ApiKey, ApiKeyScope, User


@admin.register(User)
class UserAdmin(DjangoUserAdmin):
    list_display = ("username", "email", "display_name", "is_staff", "is_active")
    search_fields = ("username", "email", "display_name")
    fieldsets = DjangoUserAdmin.fieldsets + (
        ("Brainbox", {"fields": ("display_name",)}),
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
