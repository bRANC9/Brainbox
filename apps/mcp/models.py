import uuid

from django.conf import settings
from django.db import models


class OAuthClient(models.Model):
    """Public OAuth client registered for the MCP authorization flow."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    client_id = models.CharField(max_length=64, unique=True)
    client_name = models.CharField(max_length=255)
    application_type = models.CharField(max_length=16, default="native")
    redirect_uris = models.JSONField(default=list)
    grant_types = models.JSONField(default=list)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["client_name"]

    def __str__(self) -> str:
        return self.client_name or self.client_id


class AuthorizationCode(models.Model):
    """Short-lived, one-use authorization code. Only its keyed digest is stored."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    code_hash = models.CharField(max_length=64, unique=True)
    client = models.ForeignKey(OAuthClient, on_delete=models.CASCADE)
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE)
    redirect_uri = models.TextField()
    code_challenge = models.CharField(max_length=128)
    resource = models.TextField()
    scope = models.CharField(max_length=255, default="mcp")
    expires_at = models.DateTimeField(db_index=True)
    used_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self) -> str:
        return f"Authorization code {self.pk}"


class AccessToken(models.Model):
    """Opaque MCP access token bound to a user, client and resource URI."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    token_hash = models.CharField(max_length=64, unique=True)
    client = models.ForeignKey(OAuthClient, on_delete=models.CASCADE)
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE)
    family_id = models.UUIDField(null=True, blank=True, db_index=True)
    resource = models.TextField()
    scope = models.CharField(max_length=255, default="mcp")
    expires_at = models.DateTimeField(db_index=True)
    revoked_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    last_used_at = models.DateTimeField(null=True, blank=True)

    def __str__(self) -> str:
        return f"Access token {self.pk}"


class RefreshToken(models.Model):
    """Rotating refresh token; reuse revokes its full token family."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    token_hash = models.CharField(max_length=64, unique=True)
    client = models.ForeignKey(OAuthClient, on_delete=models.CASCADE)
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE)
    family_id = models.UUIDField(db_index=True)
    resource = models.TextField()
    scope = models.CharField(max_length=255, default="mcp")
    expires_at = models.DateTimeField(db_index=True)
    used_at = models.DateTimeField(null=True, blank=True)
    revoked_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self) -> str:
        return f"Refresh token {self.pk}"
