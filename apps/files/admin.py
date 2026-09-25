from django.contrib import admin

from .models import File, FileVersion


@admin.register(File)
class FileAdmin(admin.ModelAdmin):
    list_display = ("name", "workspace", "project", "mime_type", "size", "updated_at")
    list_filter = ("workspace", "mime_type")
    search_fields = ("name", "path")


@admin.register(FileVersion)
class FileVersionAdmin(admin.ModelAdmin):
    list_display = ("file", "version", "size", "checksum", "created_by", "created_at")
    search_fields = ("file__name",)
