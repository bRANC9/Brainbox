from django.contrib import admin

from .models import RuntimeSetting


@admin.register(RuntimeSetting)
class RuntimeSettingAdmin(admin.ModelAdmin):
    list_display = ("key", "value_preview", "updated_by", "updated_at")
    search_fields = ("key",)
    readonly_fields = ("created_at", "updated_at", "value_preview")

    @admin.display(description="érték")
    def value_preview(self, obj):
        return "***" if obj.value else "(empty)"
