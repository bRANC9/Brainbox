from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


def backfill_owners(apps, schema_editor):
    """owner = created_by, so the creator keeps exactly the access they had.

    The owner's ADMIN ACL entry is not touched: it already exists (issued by
    WorkspaceService.create), it just had no field pointing at it. Rows with no
    creator cannot happen through the services (``_require_actor`` refuses
    them) but a NULL owner is tolerated on purpose - it marks an orphaned
    resource whose ACL only an audited superuser takeover can reach.
    """
    Workspace = apps.get_model("workspaces", "Workspace")
    Project = apps.get_model("workspaces", "Project")
    for model in (Workspace, Project):
        model.objects.filter(owner__isnull=True, created_by__isnull=False).update(
            owner=models.F("created_by")
        )


def clear_owners(apps, schema_editor):
    Workspace = apps.get_model("workspaces", "Workspace")
    Project = apps.get_model("workspaces", "Project")
    Workspace.objects.update(owner=None)
    Project.objects.update(owner=None)


class Migration(migrations.Migration):
    dependencies = [
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
        ("workspaces", "0001_initial"),
    ]

    operations = [
        migrations.AddField(
            model_name="workspace",
            name="kind",
            field=models.CharField(
                choices=[("shared", "Shared"), ("personal", "Personal")],
                default="shared",
                max_length=16,
            ),
        ),
        migrations.AddField(
            model_name="workspace",
            name="owner",
            field=models.ForeignKey(
                blank=True,
                help_text=(
                    "A workspace tulajdonosa: egyedül ő módosíthatja a workspace "
                    "ACL-jét, és ő adhat át tulajdont. NULL = tulajdon nélküli (örökség) sor, "
                    "amelynek ACL-jét csak auditált superuser-átvétellel lehet elérni."
                ),
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name="owned_workspaces",
                to=settings.AUTH_USER_MODEL,
            ),
        ),
        migrations.AddField(
            model_name="project",
            name="owner",
            field=models.ForeignKey(
                blank=True,
                help_text=(
                    "A projekt tulajdonosa. A létrehozó az alapértelmezett; "
                    "a tulajdon transferálható, de csak a workspace/projekt tulajdonosa "
                    "vagy auditált superuser-átvétel után."
                ),
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name="owned_projects",
                to=settings.AUTH_USER_MODEL,
            ),
        ),
        migrations.RunPython(backfill_owners, clear_owners),
        migrations.AddConstraint(
            model_name="workspace",
            constraint=models.UniqueConstraint(
                condition=models.Q(("kind", "personal")),
                fields=("owner",),
                name="uniq_personal_per_owner",
            ),
        ),
    ]
