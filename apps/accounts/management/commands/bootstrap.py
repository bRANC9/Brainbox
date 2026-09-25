"""Idempotent first-boot bootstrap: ensure a superuser and a default workspace."""

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand

from apps.workspaces.models import Workspace
from apps.workspaces.services import WorkspaceService


class Command(BaseCommand):
    help = "Create or update the bootstrap superuser (and a default workspace)."

    def add_arguments(self, parser):
        parser.add_argument("--username", required=True)
        parser.add_argument("--password", required=True)
        parser.add_argument("--email", default="")
        parser.add_argument("--workspace", default="Personal")
        parser.add_argument("--skip-workspace", action="store_true")

    def handle(self, *args, **options):
        user_model = get_user_model()
        username = options["username"]

        user, created = user_model.objects.get_or_create(
            username=username, defaults={"email": options["email"]}
        )
        if options["email"]:
            user.email = options["email"]
        user.is_staff = True
        user.is_superuser = True
        user.set_password(options["password"])
        user.save()

        action = "created" if created else "updated"
        self.stdout.write(self.style.SUCCESS(f"Superuser '{username}' {action}."))

        if options["skip_workspace"]:
            return
        if Workspace.objects.exists():
            self.stdout.write("Workspace already exists, skipping default workspace.")
            return

        workspace = WorkspaceService.create(
            name=options["workspace"],
            description="Default workspace created on first boot.",
            created_by=user,
        )
        self.stdout.write(self.style.SUCCESS(f"Default workspace '{workspace.slug}' created."))
