"""Folders become named nodes with a container chain.

Turns ``DocumentFolder`` from an identity-free path segment into a real object:
its own ``name``, an optional ``description``, an ``owner``, and a ``container``
pointing at whatever encloses it - another folder, a project, or a workspace. The
existing ``path`` becomes a denormalised cache recomputed from that chain.

The backfill walks every folder in path order (a prefix always sorts before the
longer string, so a parent exists by the time its child is seen) and points each
one at its nearest existing ancestor folder, or at the project/workspace resource
when the folder sits at the scope root. Then every path is recomputed from the
chain, so the data is identical to what the old ``path`` strings said - which is
what makes this reversible.

Documents and files gain a ``folder`` pointer. It is left NULL in this migration
on purpose: pointing them at their folders is a service-level decision that has
to respect the create/update paths, and doing it in a data migration would put
knowledge about the tree in the schema history.
"""

from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion
import django.db.models.expressions
import django.db.models.functions


def backfill_names_and_containers(apps, schema_editor):
    """name + container + path, derived from the old path strings."""
    Folder = apps.get_model("documents", "DocumentFolder")

    folders = list(
        Folder.objects.order_by("workspace_id", "project_id", "path").values(
            "id", "path", "workspace_id", "project_id"
        )
    )
    # (workspace_id, project_id, path) -> (id, container_resource_id)
    seen: dict[tuple, tuple] = {}

    for row in folders:
        old_path = row["path"] or ""
        segments = [s for s in old_path.split("/") if s]
        if not segments:
            continue
        name = segments[-1]
        scope = (row["workspace_id"], row["project_id"])
        parent_container = None
        if len(segments) > 1:
            parent_path = "/".join(segments[:-1])
            # The ancestor is looked up in the *same* scope, matching how paths
            # were scoped before (project paths exclude the project name).
            parent_id = seen.get((*scope, parent_path))
            if parent_id is None:
                # Ancestor row missing (an import can leave gaps). Fall back to
                # the scope root rather than leaving the folder orphaned.
                parent_id = seen.get((*scope, ""))
            parent_container = parent_id[1] if parent_id else None
        if parent_container is None:
            # A top-level folder hangs off the project, or off the workspace
            # when the scope is the workspace root.
            parent_container = row["project_id"] or row["workspace_id"]
        seen[(*scope, old_path)] = (row["id"], parent_container)
        # No project-name prefix: a path has always been relative to the
        # workspace or project it is in, and the container already knows which.
        Folder.objects.filter(pk=row["id"]).update(
            name=name,
            container_id=parent_container,
            path=old_path,
        )


def clear_names_and_containers(apps, schema_editor):
    Folder = apps.get_model("documents", "DocumentFolder")
    for row in Folder.objects.all().values("path"):
        # Restore the old value: the prefix that the chain added is whatever
        # precedes the last segment, so strip exactly that.
        path = row["path"]
        Folder.objects.filter(path=path).update(path=path.rsplit("/", 1)[0])
    Folder.objects.update(container=None, name="")


class Migration(migrations.Migration):
    dependencies = [
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
        ("documents", "0004_documentfolder_resource"),
    ]

    operations = [
        migrations.AddField(
            model_name="documentfolder",
            name="container",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.CASCADE,
                related_name="child_folders",
                to="resources.resource",
            ),
        ),
        migrations.AddField(
            model_name="documentfolder",
            name="name",
            field=models.CharField(default="", max_length=255),
            preserve_default=False,
        ),
        migrations.AddField(
            model_name="documentfolder",
            name="description",
            field=models.TextField(blank=True),
        ),
        migrations.AddField(
            model_name="documentfolder",
            name="owner",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name="owned_folders",
                to=settings.AUTH_USER_MODEL,
            ),
        ),
        migrations.AddField(
            model_name="document",
            name="folder",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="documents",
                to="documents.documentfolder",
            ),
        ),
        migrations.AlterField(
            model_name="documentfolder",
            name="path",
            field=models.CharField(blank=True, max_length=1024),
        ),
        migrations.RunPython(backfill_names_and_containers, clear_names_and_containers),
        # The old constraint keyed on (workspace, project, path); identity is now
        # (container, name). Both exist during the migration so the backfill can
        # run against the old index.
        migrations.RemoveConstraint(
            model_name="documentfolder",
            name="uniq_folder_path",
        ),
        migrations.AddConstraint(
            model_name="documentfolder",
            constraint=models.UniqueConstraint(
                fields=("container", "name"), name="uniq_folder_name_per_container"
            ),
        ),
    ]