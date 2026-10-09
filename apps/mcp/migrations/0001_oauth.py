import uuid

import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):
    initial = True

    dependencies = [migrations.swappable_dependency(settings.AUTH_USER_MODEL)]

    operations = [
        migrations.CreateModel(
            name="OAuthClient",
            fields=[
                ("id", models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ("client_id", models.CharField(max_length=64, unique=True)),
                ("client_name", models.CharField(max_length=255)),
                ("application_type", models.CharField(default="native", max_length=16)),
                ("redirect_uris", models.JSONField(default=list)),
                ("grant_types", models.JSONField(default=list)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
            ],
            options={"ordering": ["client_name"]},
        ),
        migrations.CreateModel(
            name="AuthorizationCode",
            fields=[
                ("id", models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ("code_hash", models.CharField(max_length=64, unique=True)),
                ("redirect_uri", models.TextField()),
                ("code_challenge", models.CharField(max_length=128)),
                ("resource", models.TextField()),
                ("scope", models.CharField(default="mcp", max_length=255)),
                ("expires_at", models.DateTimeField(db_index=True)),
                ("used_at", models.DateTimeField(blank=True, null=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("client", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, to="mcp.oauthclient")),
                ("user", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, to=settings.AUTH_USER_MODEL)),
            ],
        ),
        migrations.CreateModel(
            name="AccessToken",
            fields=[
                ("id", models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ("token_hash", models.CharField(max_length=64, unique=True)),
                ("resource", models.TextField()),
                ("scope", models.CharField(default="mcp", max_length=255)),
                ("expires_at", models.DateTimeField(db_index=True)),
                ("revoked_at", models.DateTimeField(blank=True, null=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("last_used_at", models.DateTimeField(blank=True, null=True)),
                ("family_id", models.UUIDField(blank=True, db_index=True, null=True)),
                ("client", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, to="mcp.oauthclient")),
                ("user", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, to=settings.AUTH_USER_MODEL)),
            ],
        ),
        migrations.CreateModel(
            name="RefreshToken",
            fields=[
                ("id", models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ("token_hash", models.CharField(max_length=64, unique=True)),
                ("family_id", models.UUIDField(db_index=True)),
                ("resource", models.TextField()),
                ("scope", models.CharField(default="mcp", max_length=255)),
                ("expires_at", models.DateTimeField(db_index=True)),
                ("used_at", models.DateTimeField(blank=True, null=True)),
                ("revoked_at", models.DateTimeField(blank=True, null=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("client", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, to="mcp.oauthclient")),
                ("user", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, to=settings.AUTH_USER_MODEL)),
            ],
        ),
    ]
