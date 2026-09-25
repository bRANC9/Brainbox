from django.urls import path

from . import oidc

app_name = "accounts"

urlpatterns = [
    path("oidc/login/", oidc.oidc_login, name="oidc_login"),
    path("oidc/callback/", oidc.oidc_callback, name="oidc_callback"),
]
