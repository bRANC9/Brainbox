"""OAuth 2.1 authorization-code flow for HTTP MCP clients."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import re
import secrets
import uuid
from datetime import timedelta
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from django.conf import settings
from django.contrib.auth.views import redirect_to_login
from django.core import signing
from django.db import transaction
from django.http import JsonResponse
from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.cache import never_cache
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_GET, require_http_methods, require_POST

from .models import AccessToken, AuthorizationCode, OAuthClient, RefreshToken

AUTH_CODE_TTL = timedelta(minutes=5)
ACCESS_TOKEN_TTL = timedelta(hours=1)
REFRESH_TOKEN_TTL = timedelta(days=30)
CODE_VERIFIER_RE = re.compile(r"^[A-Za-z0-9._~-]{43,128}$")
CODE_CHALLENGE_RE = re.compile(r"^[A-Za-z0-9_-]{43}$")


def _digest(value: str) -> str:
    return hmac.new(settings.SECRET_KEY.encode(), value.encode(), hashlib.sha256).hexdigest()


def _origin(request) -> str:
    return request.build_absolute_uri("/").rstrip("/")


def mcp_resource_uri(request) -> str:
    return request.build_absolute_uri(reverse("mcp:endpoint"))


def protected_resource_metadata_url(request) -> str:
    return request.build_absolute_uri("/.well-known/oauth-protected-resource/mcp")


def _json_error(error: str, description: str, status: int = 400) -> JsonResponse:
    response = JsonResponse({"error": error, "error_description": description}, status=status)
    response["Cache-Control"] = "no-store"
    response["Pragma"] = "no-cache"
    return response


def _oauth_metadata(request) -> dict:
    origin = _origin(request)
    return {
        "issuer": origin,
        "authorization_endpoint": request.build_absolute_uri(reverse("mcp:oauth_authorize")),
        "token_endpoint": request.build_absolute_uri(reverse("mcp:oauth_token")),
        "revocation_endpoint": request.build_absolute_uri(reverse("mcp:oauth_revoke")),
        "registration_endpoint": request.build_absolute_uri(reverse("mcp:oauth_register")),
        "response_types_supported": ["code"],
        "grant_types_supported": ["authorization_code", "refresh_token"],
        "code_challenge_methods_supported": ["S256"],
        "token_endpoint_auth_methods_supported": ["none"],
        "scopes_supported": ["mcp", "offline_access"],
        "authorization_response_iss_parameter_supported": True,
        "revocation_endpoint_auth_methods_supported": ["none"],
    }


@require_GET
def authorization_server_metadata(request):
    response = JsonResponse(_oauth_metadata(request))
    response["Cache-Control"] = "public, max-age=3600"
    return response


@require_GET
def protected_resource_metadata(request):
    response = JsonResponse(
        {
            "resource": mcp_resource_uri(request),
            "authorization_servers": [_origin(request)],
            "scopes_supported": ["mcp"],
            "bearer_methods_supported": ["header"],
        }
    )
    response["Cache-Control"] = "public, max-age=3600"
    return response


def _valid_redirect_uri(value: str) -> bool:
    if not isinstance(value, str) or len(value) > 1000:
        return False
    try:
        parsed = urlsplit(value)
        _ = parsed.port  # Reject malformed ports.
    except ValueError:
        return False
    if parsed.fragment or parsed.username or parsed.password or not parsed.hostname:
        return False
    if any(key in {"code", "state", "error", "iss"} for key, _ in parse_qsl(parsed.query)):
        return False
    if parsed.scheme == "https":
        return True
    return parsed.scheme == "http" and parsed.hostname.lower() in {
        "localhost",
        "127.0.0.1",
        "::1",
    }


def _is_loopback_redirect_uri(value: str) -> bool:
    parsed = urlsplit(value)
    return parsed.scheme == "http" and parsed.hostname.lower() in {
        "localhost",
        "127.0.0.1",
        "::1",
    }


@csrf_exempt
@require_POST
def register_client(request):
    try:
        body = json.loads(request.body.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return _json_error("invalid_client_metadata", "The registration body must be valid JSON.")
    if not isinstance(body, dict):
        return _json_error("invalid_client_metadata", "The registration body must be a JSON object.")

    name = body.get("client_name")
    redirect_uris = body.get("redirect_uris")
    if not isinstance(name, str) or not name.strip() or len(name.strip()) > 255:
        return _json_error("invalid_client_metadata", "A client_name of 1 to 255 characters is required.")
    if (
        not isinstance(redirect_uris, list)
        or not 1 <= len(redirect_uris) <= 10
        or any(not _valid_redirect_uri(uri) for uri in redirect_uris)
        or len(set(redirect_uris)) != len(redirect_uris)
    ):
        return _json_error(
            "invalid_redirect_uri",
            "redirect_uris must contain 1 to 10 unique HTTPS or loopback HTTP URLs.",
        )
    application_type = body.get("application_type")
    if application_type is None:
        application_type = (
            "native" if any(_is_loopback_redirect_uri(uri) for uri in redirect_uris) else "web"
        )
    if not isinstance(application_type, str) or application_type not in {"native", "web"}:
        return _json_error("invalid_client_metadata", "application_type must be native or web.")
    if application_type == "web" and any(
        _is_loopback_redirect_uri(uri) for uri in redirect_uris
    ):
        return _json_error(
            "invalid_redirect_uri", "Loopback redirect URIs require application_type=native."
        )
    if body.get("token_endpoint_auth_method", "none") != "none":
        return _json_error("invalid_client_metadata", "Only public clients are supported.")
    grant_types = body.get("grant_types", ["authorization_code"])
    response_types = body.get("response_types", ["code"])
    allowed_grants = {"authorization_code", "refresh_token"}
    if (
        not isinstance(grant_types, list)
        or "authorization_code" not in grant_types
        or any(not isinstance(grant, str) or grant not in allowed_grants for grant in grant_types)
        or len(set(grant_types)) != len(grant_types)
    ):
        return _json_error("invalid_client_metadata", "The authorization_code grant is required.")
    if (
        not isinstance(response_types, list)
        or len(response_types) != 1
        or response_types[0] != "code"
    ):
        return _json_error("invalid_client_metadata", "Only the code response type is supported.")

    client_id = secrets.token_urlsafe(32)
    client = OAuthClient.objects.create(
        client_id=client_id,
        client_name=name.strip(),
        application_type=application_type,
        redirect_uris=redirect_uris,
        grant_types=grant_types,
    )
    response = JsonResponse(
        {
            "client_id": client.client_id,
            "client_id_issued_at": int(client.created_at.timestamp()),
            "client_name": client.client_name,
            "redirect_uris": client.redirect_uris,
            "grant_types": client.grant_types,
            "response_types": ["code"],
            "token_endpoint_auth_method": "none",
            "application_type": client.application_type,
        },
        status=201,
    )
    response["Cache-Control"] = "no-store"
    return response


def _authorization_request(request):
    params = request.GET
    client_id = params.get("client_id", "")
    redirect_uri = params.get("redirect_uri", "")
    client = OAuthClient.objects.filter(client_id=client_id).first()
    # Never send errors to a URI until the client and exact callback are known.
    if client is None or redirect_uri not in client.redirect_uris:
        return None, _json_error("invalid_request", "Unknown client or unregistered redirect URI.")

    state = params.get("state", "")
    response_type = params.get("response_type", "")
    challenge = params.get("code_challenge", "")
    challenge_method = params.get("code_challenge_method", "")
    resource = params.get("resource", "")
    scope = params.get("scope", "mcp")

    error = None
    if response_type != "code":
        error = "unsupported_response_type"
    elif not state or len(state) > 500:
        error = "invalid_request"
    elif challenge_method != "S256" or not CODE_CHALLENGE_RE.fullmatch(challenge):
        error = "invalid_request"
    elif resource != mcp_resource_uri(request):
        error = "invalid_target"
    else:
        requested_scopes = scope.split()
        if (
            "mcp" not in requested_scopes
            or set(requested_scopes) - {"mcp", "offline_access"}
            or len(requested_scopes) != len(set(requested_scopes))
            or (
                "offline_access" in requested_scopes
                and "refresh_token" not in client.grant_types
            )
        ):
            error = "invalid_scope"
    if error:
        return None, _authorization_error_redirect(redirect_uri, error, state, _origin(request))

    return {
        "client_id": client.client_id,
        "redirect_uri": redirect_uri,
        "state": state,
        "response_type": response_type,
        "code_challenge": challenge,
        "resource": resource,
        "scope": "mcp"
        + (" offline_access" if "offline_access" in scope.split() else ""),
    }, None


def _authorization_error_redirect(redirect_uri: str, error: str, state: str, issuer: str):
    parsed = urlsplit(redirect_uri)
    query = parse_qsl(parsed.query, keep_blank_values=True)
    query.extend([("error", error), ("state", state), ("iss", issuer)])
    return redirect(urlunsplit((parsed.scheme, parsed.netloc, parsed.path, urlencode(query), "")))


@require_http_methods(["GET", "POST"])
@never_cache
def authorize(request):
    if request.method == "GET":
        params, error = _authorization_request(request)
        if error:
            return error
        if not request.user.is_authenticated:
            return redirect_to_login(request.get_full_path())
        signed_request = signing.dumps(
            {**params, "user_id": str(request.user.pk)}, salt="mcp-oauth-authorization"
        )
        return render(
            request,
            "mcp/oauth_consent.html",
            {"client_name": OAuthClient.objects.get(client_id=params["client_id"]).client_name,
             "authorization": signed_request,
             "offline_access": "offline_access" in params["scope"].split()},
        )

    if not request.user.is_authenticated:
        return redirect_to_login(request.get_full_path())
    try:
        params = signing.loads(
            request.POST.get("authorization", ""),
            salt="mcp-oauth-authorization",
            max_age=600,
        )
    except signing.BadSignature:
        return _json_error("invalid_request", "The authorization request expired. Please start again.")
    if params.get("user_id") != str(request.user.pk):
        return _json_error("invalid_request", "The authorization request belongs to another session.")
    client = OAuthClient.objects.filter(client_id=params.get("client_id")).first()
    redirect_uri = params.get("redirect_uri", "")
    if client is None or redirect_uri not in client.redirect_uris:
        return _json_error("invalid_request", "The client registration is no longer valid.")

    if request.POST.get("decision") != "allow":
        return _authorization_error_redirect(
            redirect_uri, "access_denied", params["state"], _origin(request)
        )

    raw_code = secrets.token_urlsafe(32)
    AuthorizationCode.objects.create(
        code_hash=_digest(raw_code),
        client=client,
        user=request.user,
        redirect_uri=redirect_uri,
        code_challenge=params["code_challenge"],
        resource=params["resource"],
        scope=params["scope"],
        expires_at=timezone.now() + AUTH_CODE_TTL,
    )
    parsed = urlsplit(redirect_uri)
    query = parse_qsl(parsed.query, keep_blank_values=True)
    query.extend([("code", raw_code), ("state", params["state"]), ("iss", _origin(request))])
    return redirect(urlunsplit((parsed.scheme, parsed.netloc, parsed.path, urlencode(query), "")))


def _pkce_challenge(verifier: str) -> str:
    digest = hashlib.sha256(verifier.encode()).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


@csrf_exempt
@require_POST
def token(request):
    grant_type = request.POST.get("grant_type")
    if grant_type == "refresh_token":
        return _refresh_access_token(request)
    if grant_type != "authorization_code":
        return _json_error("unsupported_grant_type", "The requested grant is not supported.")

    code_value = request.POST.get("code", "")
    client_id = request.POST.get("client_id", "")
    redirect_uri = request.POST.get("redirect_uri", "")
    verifier = request.POST.get("code_verifier", "")
    resource = request.POST.get("resource", "")
    if not code_value or not client_id or not redirect_uri or not resource:
        return _json_error("invalid_request", "code, client_id, redirect_uri and resource are required.")
    if not CODE_VERIFIER_RE.fullmatch(verifier):
        return _json_error("invalid_grant", "The PKCE verifier is invalid.")
    if resource != mcp_resource_uri(request):
        return _json_error("invalid_target", "The requested resource does not match this MCP server.")

    now = timezone.now()
    with transaction.atomic():
        authorization = (
            AuthorizationCode.objects.select_for_update()
            .select_related("client", "user")
            .filter(code_hash=_digest(code_value))
            .first()
        )
        if (
            authorization is None
            or authorization.used_at is not None
            or authorization.expires_at <= now
            or authorization.client.client_id != client_id
            or authorization.redirect_uri != redirect_uri
            or authorization.resource != resource
            or not authorization.user.is_active
            or not hmac.compare_digest(
                authorization.code_challenge, _pkce_challenge(verifier)
            )
        ):
            return _json_error("invalid_grant", "The authorization code is invalid or expired.")

        claimed = AuthorizationCode.objects.filter(
            pk=authorization.pk, used_at__isnull=True, expires_at__gt=now
        ).update(used_at=now)
        if claimed != 1:
            return _json_error("invalid_grant", "The authorization code was already used.")

        access_token = secrets.token_urlsafe(48)
        family_id = None
        raw_refresh_token = None
        refresh_expires_at = None
        if "offline_access" in authorization.scope.split():
            family_id = uuid.uuid4()
            raw_refresh_token = secrets.token_urlsafe(48)
            refresh_expires_at = now + REFRESH_TOKEN_TTL
        expires_at = now + ACCESS_TOKEN_TTL
        AccessToken.objects.create(
            token_hash=_digest(access_token),
            client=authorization.client,
            user=authorization.user,
            family_id=family_id,
            resource=authorization.resource,
            scope=authorization.scope,
            expires_at=expires_at,
        )
        if raw_refresh_token:
            RefreshToken.objects.create(
                token_hash=_digest(raw_refresh_token),
                client=authorization.client,
                user=authorization.user,
                family_id=family_id,
                resource=authorization.resource,
                scope=authorization.scope,
                expires_at=refresh_expires_at,
            )

    return _token_response(access_token, authorization.scope, authorization.resource,
                           raw_refresh_token)


def _token_response(access_token: str, scope: str, resource: str, refresh_token: str | None):
    payload = {
        "access_token": access_token,
        "token_type": "Bearer",
        "expires_in": int(ACCESS_TOKEN_TTL.total_seconds()),
        "scope": scope,
        "resource": resource,
    }
    if refresh_token:
        payload["refresh_token"] = refresh_token
    response = JsonResponse(payload)
    response["Cache-Control"] = "no-store"
    response["Pragma"] = "no-cache"
    return response


def _revoke_family(family_id, now):
    RefreshToken.objects.filter(family_id=family_id).update(revoked_at=now)
    AccessToken.objects.filter(family_id=family_id, revoked_at__isnull=True).update(
        revoked_at=now
    )


def _refresh_access_token(request):
    raw_refresh_token = request.POST.get("refresh_token", "")
    client_id = request.POST.get("client_id", "")
    resource = request.POST.get("resource", "")
    if not raw_refresh_token or not client_id or not resource:
        return _json_error("invalid_request", "refresh_token, client_id and resource are required.")
    if resource != mcp_resource_uri(request):
        return _json_error("invalid_target", "The requested resource does not match this MCP server.")

    now = timezone.now()
    with transaction.atomic():
        current = (
            RefreshToken.objects.select_for_update()
            .select_related("client", "user")
            .filter(token_hash=_digest(raw_refresh_token))
            .first()
        )
        if (
            current is None
            or current.client.client_id != client_id
            or "refresh_token" not in current.client.grant_types
            or current.resource != resource
            or current.expires_at <= now
        ):
            return _json_error("invalid_grant", "The refresh token is invalid or expired.")
        if current.used_at is not None:
            _revoke_family(current.family_id, now)
            return _json_error("invalid_grant", "Refresh token reuse detected; authorization was revoked.")
        if current.revoked_at is not None or not current.user.is_active:
            return _json_error("invalid_grant", "The refresh token is revoked or its user is inactive.")
        requested_scope = request.POST.get("scope")
        if requested_scope and requested_scope != current.scope:
            return _json_error("invalid_scope", "Refresh requests must keep the original scope.")

        claimed = RefreshToken.objects.filter(
            pk=current.pk, used_at__isnull=True, revoked_at__isnull=True, expires_at__gt=now
        ).update(used_at=now)
        if claimed != 1:
            return _json_error("invalid_grant", "The refresh token was already used.")

        access_value = secrets.token_urlsafe(48)
        next_refresh_value = secrets.token_urlsafe(48)
        AccessToken.objects.create(
            token_hash=_digest(access_value),
            client=current.client,
            user=current.user,
            family_id=current.family_id,
            resource=current.resource,
            scope=current.scope,
            expires_at=now + ACCESS_TOKEN_TTL,
        )
        RefreshToken.objects.create(
            token_hash=_digest(next_refresh_value),
            client=current.client,
            user=current.user,
            family_id=current.family_id,
            resource=current.resource,
            scope=current.scope,
            expires_at=current.expires_at,
        )

    return _token_response(access_value, current.scope, current.resource, next_refresh_value)


@csrf_exempt
@require_POST
def revoke(request):
    raw_token = request.POST.get("token", "")
    client_id = request.POST.get("client_id", "")
    if raw_token and client_id:
        now = timezone.now()
        refresh = RefreshToken.objects.filter(
            token_hash=_digest(raw_token), client__client_id=client_id
        ).first()
        if refresh is not None:
            with transaction.atomic():
                _revoke_family(refresh.family_id, now)
        else:
            AccessToken.objects.filter(
                token_hash=_digest(raw_token), client__client_id=client_id,
                revoked_at__isnull=True,
            ).update(revoked_at=now)
    response = JsonResponse({}, status=200)
    response["Cache-Control"] = "no-store"
    return response


def authenticate_bearer(request, raw_token: str):
    """Return the token's user, or ``None`` for invalid/expired/wrong-audience tokens."""
    if not raw_token:
        return None
    token_record = (
        AccessToken.objects.select_related("user")
        .filter(
            token_hash=_digest(raw_token),
            revoked_at__isnull=True,
            expires_at__gt=timezone.now(),
            resource=mcp_resource_uri(request),
            scope__in=["mcp", "mcp offline_access"],
            user__is_active=True,
        )
        .first()
    )
    if token_record is None:
        return None
    AccessToken.objects.filter(pk=token_record.pk).update(last_used_at=timezone.now())
    return token_record.user
