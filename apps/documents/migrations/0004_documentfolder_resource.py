from django.db import migrations, models
import django.db.models.deletion


def create_folder_resources(apps, schema_editor):
    """Give every existing folder a Resource chained to its enclosing folder.

    Folders used to carry no Resource, so access could not be granted on one.
    The parent chain is what the permission engine walks, which is what makes
    "grant me A/B/C" work while "A" and "A/B" stay visible as a trail.

    Ordering by path guarantees a parent is created before its children
    (a prefix always sorts before the longer string), so a single pass suffices.
    ``project_id``/``workspace_id`` *are* Resource ids, which is what makes the
    top-level parent a one-liner.
    """
    Resource = apps.get_model("resources", "Resource")
    DocumentFolder = apps.get_model("documents", "DocumentFolder")
    by_scope: dict = {}
    folders = DocumentFolder.objects.order_by("workspace_id", "project_id", "path")
    for folder in folders.iterator():
        scope = (folder.workspace_id, folder.project_id)
        known = by_scope.setdefault(scope, {})
        parent_id = None
        if "/" in folder.path:
            parent_id = known.get(folder.path.rsplit("/", 1)[0])
        if parent_id is None:
            parent_id = folder.project_id or folder.workspace_id
        resource = Resource.objects.create(
            resource_type="folder",
            name=folder.path.rsplit("/", 1)[-1],
            parent_id=parent_id,
            created_by_id=folder.created_by_id,
            metadata={"path": folder.path},
        )
        folder.resource_id = resource.id
        folder.save(update_fields=["resource"])
        known[folder.path] = resource.id


def drop_folder_resources(apps, schema_editor):
    Resource = apps.get_model("resources", "Resource")
    Resource.objects.filter(resource_type="folder").delete()


class Migration(migrations.Migration):
    dependencies = [
        ("documents", "0003_document_is_template"),
        ("resources", "0002_resource_no_takeover"),
    ]

    operations = [
        migrations.AddField(
            model_name="documentfolder",
            name="resource",
            field=models.OneToOneField(
                null=True,
                on_delete=django.db.models.deletion.CASCADE,
                related_name="folder",
                to="resources.resource",
            ),
        ),
        migrations.RunPython(create_folder_resources, drop_folder_resources),
        migrations.AlterField(
            model_name="documentfolder",
            name="resource",
            field=models.OneToOneField(
                on_delete=django.db.models.deletion.CASCADE,
                related_name="folder",
                to="resources.resource",
            ),
        ),
    ]
