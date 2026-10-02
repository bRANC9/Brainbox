from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("resources", "0001_initial"),
    ]

    operations = [
        migrations.AlterField(
            model_name="resource",
            name="resource_type",
            field=models.CharField(
                choices=[
                    ("workspace", "Workspace"),
                    ("project", "Project"),
                    ("folder", "Folder"),
                    ("document", "Document"),
                    ("file", "File"),
                    ("git_repository", "Git repository"),
                    ("secret", "Secret"),
                    ("mcp_server", "MCP server"),
                    ("api_key", "API key"),
                ],
                db_index=True,
                max_length=32,
            ),
        ),
        migrations.AddField(
            model_name="resource",
            name="no_takeover",
            field=models.BooleanField(
                default=False,
                help_text=(
                    "Zárja ki a superuser 'jogosultság átvétel' útját. Öröklődik: "
                    "ha egy workspace-en be van kapcsolva, az egész fán nem lehet "
                    "ADMIN-t szerezni. Csak a workspace tulajdonosa kapcsolhatja be."
                ),
            ),
        ),
    ]
