"""Root URL configuration."""

from django.conf import settings
from django.conf.urls.static import static
from django.contrib import admin
from django.urls import include, path

from apps.api.health import healthz

urlpatterns = [
    path("admin/", admin.site.urls),
    path("healthz", healthz, name="healthz"),
    path("accounts/", include("django.contrib.auth.urls")),
    path("api/v1/", include("apps.api.urls")),
    path("", include("apps.web.urls")),
]

if settings.DEBUG:
    urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)
