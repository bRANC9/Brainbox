from django.apps import AppConfig


class SettingsStoreConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.settings_store"
    label = "settings_store"
    verbose_name = "Runtime settings"
