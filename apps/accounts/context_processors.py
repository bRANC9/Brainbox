from django.conf import settings


def auth_options(request):
    return {
        "oidc_enabled": settings.OIDC_ENABLED,
    }
