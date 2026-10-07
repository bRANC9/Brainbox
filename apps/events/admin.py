from django.contrib import admin

from .models import SavedSearch, Webhook, WebhookDelivery


@admin.register(Webhook)
class WebhookAdmin(admin.ModelAdmin):
    list_display = ("name", "owner", "workspace", "enabled", "created_at")
    list_filter = ("enabled",)
    search_fields = ("name",)


@admin.register(WebhookDelivery)
class WebhookDeliveryAdmin(admin.ModelAdmin):
    list_display = ("event", "webhook", "status", "attempts", "created_at", "sent_at")
    list_filter = ("status", "event")
    readonly_fields = [field.name for field in WebhookDelivery._meta.fields]


@admin.register(SavedSearch)
class SavedSearchAdmin(admin.ModelAdmin):
    list_display = ("name", "owner", "workspace", "enabled", "last_checked_at")
    list_filter = ("enabled",)
    search_fields = ("name", "query")
