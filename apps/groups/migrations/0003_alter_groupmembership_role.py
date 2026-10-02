from django.db import migrations, models


class Migration(migrations.Migration):
    """Drop the unused OWNER role: a group's managers are its owners.

    The role was stored but never read - ``PermissionService._group_ids`` only
    looks at which groups a user is in. Rather than keep a role that grants
    nothing, membership is now just member / manager, and *manager* is the level
    that can administer the group (add and remove members). Existing rows are
    untouched: "owner" simply becomes "manager", which is the level it was
    always meant to mean.
    """

    dependencies = [
        ("groups", "0002_alter_group_options_alter_groupmembership_options"),
    ]

    operations = [
        migrations.AlterField(
            model_name="groupmembership",
            name="role",
            field=models.CharField(
                choices=[("member", "Member"), ("manager", "Manager")],
                default="member",
                max_length=16,
            ),
        ),
    ]
