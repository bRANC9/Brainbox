"""Align a project node's stored path with its name.

``0006_projects_as_folders`` set a project node's own ``path`` to empty, on the
reasoning that a project *is* the storage scope so its path relative to that
scope is nothing. But ``recompute_path`` - the model's single writer - derives a
project node's path as its name, and the tree keys rows on the path, so the empty
value made every project collapse into one nameless row. The current writer wins:
a project node's path is its name.
"""

from django.db import migrations


def forwards(apps, schema_editor):
    DocumentFolder = apps.get_model("documents", "DocumentFolder")
    for folder in DocumentFolder.objects.filter(role="project").iterator():
        if folder.path != folder.name:
            folder.path = folder.name
            folder.save(update_fields=["path"])


def backwards(apps, schema_editor):
    DocumentFolder = apps.get_model("documents", "DocumentFolder")
    DocumentFolder.objects.filter(role="project").update(path="")


class Migration(migrations.Migration):
    dependencies = [("documents", "0007_documentcomment")]

    operations = [migrations.RunPython(forwards, backwards)]
