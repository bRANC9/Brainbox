"""Projects become nodes in the folder tree.

Every ``Project`` gets a ``DocumentFolder`` row that **shares the project's own
Resource**, so the node and the project are one object rather than two rows
pointing at each other. The project's existing folders are re-parented to it.

Purely additive, and deliberately path-neutral: a stored path is relative to the
storage scope, and a project already *is* a scope (``workspaces/<id>/projects/
<id>/``), so prefixing its children's paths with the project name would be
redundant and would disagree with the bytes on disk. The chain carries the
project; the path stays what it was. That is what lets a project be a folder
without moving a single file - so there is no ``flatten_projects`` step and
nothing to reverse.

Nothing is removed. ``Document.project`` / ``File.project`` /
``DocumentFolder.project`` stay, still correct, so every existing caller keeps
working unchanged. ``AuditEvent.project`` is never dropped either: an event that
happened in a project is a historical fact about where it happened, not a
pointer to a table.
"""

from django.db import migrations, models


def projects_become_nodes(apps, schema_editor):
    """One folder node per project, sharing its Resource.

    The project's existing folders are re-parented to that node and their
    container chain is fixed, but their stored ``path`` is **not** rewritten: a
    path is relative to the storage scope, and a project already *is* that
    scope, so the prefix would be redundant (and the on-disk bytes already live
    under the project's own directory).

    Nothing is removed here. Every ``project`` FK stays exactly as it was, so
    this is purely additive: the tree can now show projects as nodes, and
    nothing that reads a path, the disk or the API has changed.
    """


    Folder = apps.get_model("documents", "DocumentFolder")
    Project = apps.get_model("workspaces", "Project")

    for project in Project.objects.all().order_by("created_at"):
        workspace_id = project.workspace_id
        existing = Folder.objects.filter(resource_id=project.resource_id).first()
        if existing is None:
            existing = Folder.objects.create(
                resource_id=project.resource_id,
                container_id=workspace_id,
                name=project.name,
                role="project",
                description=project.description or "",
                workspace_id=workspace_id,
                # NULL on purpose: the node *is* the project, not something
                # inside one. Pointing it at itself would list the project as a
                # folder of itself on the project's own page.
                project_id=None,
                path="",
                owner_id=project.owner_id,
                created_by_id=project.created_by_id,
            )
        else:
            existing.project_id = None
        existing.container_id = workspace_id
        existing.path = ""
        existing.save(update_fields=["container", "path", "project"])

        # Top-level folders of this project now hang off the project node. Their
        # path is unchanged - it is already relative to the project's scope.
        for folder in Folder.objects.filter(
            workspace_id=workspace_id, project_id=project.pk
        ).exclude(resource_id=project.resource_id):
            folder.container_id = project.resource_id
            folder.save(update_fields=["container"])


class Migration(migrations.Migration):
    dependencies = [
        ("workspaces", "0002_workspace_kind_owner_project_owner"),
        ("documents", "0005_folder_nodes"),
        ("files", "0002_file_folder"),
    ]

    operations = [
        migrations.AddField(
            model_name="documentfolder",
            name="role",
            field=models.CharField(
                choices=[("folder", "Folder"), ("project", "Project")],
                default="folder",
                help_text=(
                    "A 'project' szerepű mappa önálló doboz: külön nevet visel, "
                    "külön jogosultsága van, és a fában is látszik."
                ),
                max_length=16,
            ),
        ),
        migrations.RunPython(projects_become_nodes, migrations.RunPython.noop),
    ]