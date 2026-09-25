"""Seed the default job set on a fresh database (idempotent)."""

from django.db import migrations

from apps.jobs.models import DEFAULT_JOBS


def seed(apps, schema_editor):
    Job = apps.get_model("jobs", "Job")
    for job_def in DEFAULT_JOBS:
        Job.objects.get_or_create(name=job_def["name"], defaults=job_def)


def unseed(apps, schema_editor):
    Job = apps.get_model("jobs", "Job")
    Job.objects.filter(name__in=[job_def["name"] for job_def in DEFAULT_JOBS]).delete()


class Migration(migrations.Migration):
    dependencies = [("jobs", "0001_initial")]

    operations = [migrations.RunPython(seed, unseed)]
