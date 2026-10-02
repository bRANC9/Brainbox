"""Idempotent first-boot bootstrap: ensure a superuser and its Personal workspace."""

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand

from apps.workspaces.models import Workspace
from apps.workspaces.personal import PersonalWorkspaceService


class Command(BaseCommand):
    help = "Create or update the bootstrap superuser and its Personal workspace."

    def add_arguments(self, parser):
        parser.add_argument("--username", required=True)
        parser.add_argument("--password", required=True)
        parser.add_argument("--email", default="")
        # Kept for backwards compatibility with older invocations. The default
        # workspace is no longer a *shared* one called "Personal": a workspace
        # whose name says "Personal" while everybody can see it is exactly the
        # confusion this project is fixing. The bootstrap user gets a real
        # Personal workspace instead, and every other user gets theirs on login.
        parser.add_argument("--workspace", default="", help="(deprecated, unused)")
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
            self.stdout.write("A workspace already exists, skipping.")
            return

        workspace = PersonalWorkspaceService.get_or_create(user)
        self.stdout.write(
            self.style.SUCCESS(f"Personal workspace '{workspace.slug}' created.")
        )
