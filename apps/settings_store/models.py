import uuid

from django.conf import settings
from django.db import models


class RuntimeSetting(models.Model):
    """A UI-managed override of an environment setting.

    Environment / compose values remain the default; anything set here wins.
    Secret values (tokens, API keys) are stored as-is but never returned by the
    API or rendered back into the form.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    key = models.CharField(max_length=100, unique=True)
    value = models.TextField(blank=True)
    updated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="setting_changes",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "settings_store_runtime_setting"
        ordering = ["key"]

    def __str__(self) -> str:
        return f"{self.key}={'***' if self.value else '(empty)'}"
