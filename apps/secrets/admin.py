from django.contrib import admin

from .models import Secret, SecretAttachment


class SecretAttachmentInline(admin.TabularInline):
    model = SecretAttachment
    extra = 0


@admin.register(Secret)
class SecretAdmin(admin.ModelAdmin):
    list_display = ("name", "owner", "secret_type", "is_active", "last_used_at", "created_at")
    list_filter = ("secret_type", "is_active")
    search_fields = ("name", "owner__username")
    readonly_fields = ("encrypted_payload", "fingerprint", "last_used_at", "created_at", "updated_at")
    inlines = [SecretAttachmentInline]


@admin.register(SecretAttachment)
class SecretAttachmentAdmin(admin.ModelAdmin):
    list_display = ("secret", "workspace", "project", "created_at")
