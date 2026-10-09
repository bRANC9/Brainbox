from django.urls import path

from . import oauth, views

app_name = "mcp"

urlpatterns = [
    path("", views.mcp_endpoint, name="endpoint"),
    path("oauth/register", oauth.register_client, name="oauth_register"),
    path("oauth/authorize", oauth.authorize, name="oauth_authorize"),
    path("oauth/token", oauth.token, name="oauth_token"),
    path("oauth/revoke", oauth.revoke, name="oauth_revoke"),
]
