"""Files point at their folder, like documents do.

Separate from ``documents/0005`` because a migration may only touch models in its
own app, and the field belongs to the ``files`` app while the table it points at
lives in ``documents``.
"""

from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):
    dependencies = [
        ("files", "0001_initial"),
        ("documents", "0005_folder_nodes"),
    ]

    operations = [
        migrations.AddField(
            model_name="file",
            name="folder",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="files",
                to="documents.documentfolder",
            ),
        ),
    ]