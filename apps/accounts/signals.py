"""Signals that keep per-user scaffolding in place.

Provisioning on ``user_logged_in`` is the single hook that covers both login
paths: the local form (``django.contrib.auth.urls``) and OIDC, which calls
``django.contrib.auth.login`` too. Doing it here means there is one place to
change, rather than remembering to add it to every future login method.

Failures are logged and swallowed: a missing Personal workspace must never stop
somebody from signing in - the ``web:personal_workspace`` view provisions it on
demand as a second line of defence.
"""

import logging

from django.contrib.auth.signals import user_logged_in
from django.dispatch import receiver

logger = logging.getLogger(__name__)


@receiver(user_logged_in)
def ensure_personal_workspace(sender, request, user, **kwargs):
    from apps.workspaces.personal import PersonalWorkspaceService

    try:
        PersonalWorkspaceService.get_or_create(user)
    except Exception:  # noqa: BLE001 - never block a login on provisioning
        logger.exception("personal workspace provisioning failed for %s", user)
