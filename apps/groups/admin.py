from django.contrib import admin

from .models import Group, GroupMembership


class GroupMembershipInline(admin.TabularInline):
    model = GroupMembership
    extra = 0
    autocomplete_fields = ("user",)


@admin.register(Group)
class GroupAdmin(admin.ModelAdmin):
    list_display = ("name", "description", "created_at")
    search_fields = ("name",)
    inlines = [GroupMembershipInline]


@admin.register(GroupMembership)
class GroupMembershipAdmin(admin.ModelAdmin):
    list_display = ("user", "group", "role", "created_at")
    list_filter = ("role",)
    search_fields = ("user__username", "group__name")
