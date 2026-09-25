"""Minimal OIDC (authorization code) login.

Fully optional and disabled unless ``OIDC_ENABLED`` is true. The user identity
is stored on the custom User (oidc_issuer + oidc_subject), independent of the
local Django credentials.
"""

from __future__ import annotations

import json
import secrets
import urllib.error
import urllib.parse
import urllib.request

from django.conf import settings
from django.contrib.auth import login
from django.contrib.auth.backends import ModelBackend
from django.http import Http404
from django.shortcuts import redirect
from django.urls import reverse
from django.views.decorators.http import require_http_methods

from .models import User


class OIDCError(RuntimeError):
    pass


class OIDCBackend(ModelBackend):
    """Backend marker so OIDC users can be logged in with an explicit backend."""


def _endpoint(configured: str, suffix: str) -> str:
    if configured:
        return configured
    if not settings.OIDC_ISSUER:
        raise OIDCError("OIDC_ISSUER is not configured.")
    return f"{settings.OIDC_ISSUER}{suffix}"


def _redirect_uri(request) -> str:
    if settings.OIDC_REDIRECT_URI:
        return settings.OIDC_REDIRECT_URI
    return request.build_absolute_uri(reverse("accounts:oidc_callback"))


@require_http_methods(["GET"])
def oidc_login(request):
    if not settings.OIDC_ENABLED:
        raise Http404

    state = secrets.token_urlsafe(24)
    nonce = secrets.token_urlsafe(24)
    request.session["oidc_state"] = state
    request.session["oidc_nonce"] = nonce

    params = {
        "response_type": "code",
        "client_id": settings.OIDC_CLIENT_ID,
        "redirect_uri": _redirect_uri(request),
        "scope": settings.OIDC_SCOPES,
        "state": state,
        "nonce": nonce,
    }
    authorize = _endpoint(settings.OIDC_AUTHORIZE_ENDPOINT, "/authorize")
    return redirect(f"{authorize}?{urllib.parse.urlencode(params)}")


def exchange_code(code: str, redirect_uri: str) -> dict:
    token_endpoint = _endpoint(settings.OIDC_TOKEN_ENDPOINT, "/token")
    payload = urllib.parse.urlencode(
        {
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": redirect_uri,
            "client_id": settings.OIDC_CLIENT_ID,
            "client_secret": settings.OIDC_CLIENT_SECRET,
        }
    ).encode("utf-8")
    request = urllib.request.Request(
        token_endpoint,
        data=payload,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return json.loads(response.read().decode("utf-8"))
    except (urllib.error.URLError, urllib.error.HTTPError, ValueError) as exc:
        raise OIDCError(f"Token exchange failed: {exc}") from exc


def fetch_userinfo(access_token: str) -> dict:
    userinfo_endpoint = _endpoint(settings.OIDC_USERINFO_ENDPOINT, "/userinfo")
    request = urllib.request.Request(
        userinfo_endpoint, headers={"Authorization": f"Bearer {access_token}"}
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return json.loads(response.read().decode("utf-8"))
    except (urllib.error.URLError, urllib.error.HTTPError, ValueError) as exc:
        raise OIDCError(f"Userinfo request failed: {exc}") from exc


def get_or_create_user(claims: dict) -> User:
    subject = str(claims.get("sub") or "").strip()
    if not subject:
        raise OIDCError("The identity provider did not return a subject.")

    issuer = settings.OIDC_ISSUER
    user = User.objects.filter(oidc_issuer=issuer, oidc_subject=subject).first()
    if user is not None:
        return user

    email = (claims.get("email") or "").strip().lower()
    if email:
        user = User.objects.filter(email__iexact=email).first()
        if user is not None:
            user.oidc_issuer = issuer
            user.oidc_subject = subject
            user.save(update_fields=["oidc_issuer", "oidc_subject"])
            return user

    if not settings.OIDC_AUTO_CREATE_USERS:
        raise OIDCError("No matching user and auto-creation is disabled.")

    username = email.split("@")[0] if email else f"oidc_{subject[:24]}"
    base = username
    counter = 1
    while User.objects.filter(username=username).exists():
        counter += 1
        username = f"{base}{counter}"

    user = User(
        username=username,
        email=email,
        display_name=claims.get("name") or username,
        oidc_issuer=issuer,
        oidc_subject=subject,
    )
    user.set_unusable_password()
    user.save()
    return user


@require_http_methods(["GET"])
def oidc_callback(request):
    if not settings.OIDC_ENABLED:
        raise Http404

    state = request.GET.get("state")
    if not state or state != request.session.get("oidc_state"):
        raise OIDCError("Invalid OIDC state.")

    code = request.GET.get("code")
    if not code:
        raise OIDCError("Missing authorization code.")

    token = exchange_code(code, _redirect_uri(request))
    access_token = token.get("access_token")
    if not access_token:
        raise OIDCError("No access token returned.")

    claims = fetch_userinfo(access_token)
    user = get_or_create_user(claims)

    policy = getattr(settings, "OIDC_DEFAULT_GROUPS", []) or []
    if policy:
        from apps.groups.models import Group, GroupMembership

        for group_name in policy:
            group, _ = Group.objects.get_or_create(name=group_name)
            GroupMembership.objects.get_or_create(user=user, group=group)

    login(request, user, backend="apps.accounts.oidc.OIDCBackend")
    return redirect(settings.LOGIN_REDIRECT_URL)
