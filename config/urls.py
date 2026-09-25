"""Root URL configuration."""

from django.conf import settings
from django.conf.urls.static import static
from django.contrib import admin
from django.urls import include, path

from apps.api.health import healthz
from apps.monitoring.views import readyz


def _metrics_view(request):
    """Prometheus scrape endpoint (optionally token protected)."""
    expected = settings.BRAINBOX_METRICS_TOKEN
    if expected:
        supplied = request.META.get("HTTP_AUTHORIZATION", "").removeprefix("Bearer ").strip()
        supplied = supplied or request.GET.get("token", "")
        if supplied != expected:
            from django.http import HttpResponse

            return HttpResponse("metrics endpoint disabled or unauthorized\n", status=401)
    from django_prometheus.exports import ExportToDjangoView

    return ExportToDjangoView(request)


urlpatterns = [
    path("admin/", admin.site.urls),
    path("healthz", healthz, name="healthz"),
    path("readyz", readyz, name="readyz"),
    path("metrics", _metrics_view, name="metrics"),
    path("accounts/", include("apps.accounts.urls")),
    path("accounts/", include("django.contrib.auth.urls")),
    path("api/v1/", include("apps.api.urls")),
    path("mcp", include("apps.mcp.urls")),
    path("", include("apps.web.urls")),
]

if settings.DEBUG:
    urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)
