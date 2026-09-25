"""
Django settings for the Brainbox Knowledge Platform.

Configuration is fully environment driven so the same image can run locally,
in CI and on TrueNAS without code changes.
"""

import os
from pathlib import Path

import dj_database_url

BASE_DIR = Path(__file__).resolve().parent.parent


def env_bool(name: str, default: bool = False) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def env_list(name: str, default: str = "") -> list[str]:
    raw = os.environ.get(name, default)
    return [item.strip() for item in raw.split(",") if item.strip()]


SECRET_KEY = os.environ.get("DJANGO_SECRET_KEY", "insecure-dev-key-change-me")
DEBUG = env_bool("DJANGO_DEBUG", False)
ALLOWED_HOSTS = env_list("DJANGO_ALLOWED_HOSTS", "*") or ["*"]
CSRF_TRUSTED_ORIGINS = env_list("CSRF_TRUSTED_ORIGINS", "")

# Running behind Pangolin (TLS terminated at the proxy).
USE_X_FORWARDED_HOST = True
SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
SESSION_COOKIE_SECURE = env_bool("DJANGO_SECURE_COOKIES", False)
CSRF_COOKIE_SECURE = env_bool("DJANGO_SECURE_COOKIES", False)


# ---------------------------------------------------------------------------
# Applications
# ---------------------------------------------------------------------------
DJANGO_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
]

THIRD_PARTY_APPS = [
    "rest_framework",
]

LOCAL_APPS = [
    "apps.accounts",
    "apps.groups",
    "apps.resources",
    "apps.permissions",
    "apps.workspaces",
    "apps.documents",
    "apps.files",
    "apps.links",
    "apps.git",
    "apps.embeddings",
    "apps.search",
    "apps.secrets",
    "apps.mcp",
    "apps.audit",
    "apps.api",
    "apps.web",
]

INSTALLED_APPS = DJANGO_APPS + THIRD_PARTY_APPS + LOCAL_APPS

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "whitenoise.middleware.WhiteNoiseMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

ROOT_URLCONF = "config.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [BASE_DIR / "templates"],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.debug",
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ],
        },
    },
]

WSGI_APPLICATION = "config.wsgi.application"
ASGI_APPLICATION = "config.asgi.application"


# ---------------------------------------------------------------------------
# Database (PostgreSQL in Docker, SQLite fallback for quick local runs)
# ---------------------------------------------------------------------------
DATABASE_URL = os.environ.get("DATABASE_URL")
if DATABASE_URL:
    DATABASES = {"default": dj_database_url.parse(DATABASE_URL, conn_max_age=600)}
else:
    DATABASES = {
        "default": {
            "ENGINE": "django.db.backends.sqlite3",
            "NAME": BASE_DIR / "db.sqlite3",
        }
    }


# ---------------------------------------------------------------------------
# Authentication
# ---------------------------------------------------------------------------
AUTH_USER_MODEL = "accounts.User"

AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator"},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]

LOGIN_URL = "/accounts/login/"
LOGIN_REDIRECT_URL = "/"
LOGOUT_REDIRECT_URL = "/accounts/login/"


# ---------------------------------------------------------------------------
# Internationalization
# ---------------------------------------------------------------------------
LANGUAGE_CODE = "en-us"
TIME_ZONE = "UTC"
USE_I18N = True
USE_TZ = True


# ---------------------------------------------------------------------------
# Static / media
# ---------------------------------------------------------------------------
STATIC_URL = "/static/"
STATIC_ROOT = BASE_DIR / "staticfiles"
STATICFILES_DIRS = [BASE_DIR / "static"] if (BASE_DIR / "static").exists() else []

STORAGES = {
    "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    "staticfiles": {"BACKEND": "whitenoise.storage.CompressedManifestStaticFilesStorage"},
}

MEDIA_URL = "/media/"
MEDIA_ROOT = BASE_DIR / "media"

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"


# ---------------------------------------------------------------------------
# REST framework
# ---------------------------------------------------------------------------
REST_FRAMEWORK = {
    "DEFAULT_AUTHENTICATION_CLASSES": [
        "apps.accounts.authentication.ApiKeyAuthentication",
        "rest_framework.authentication.SessionAuthentication",
    ],
    "DEFAULT_PERMISSION_CLASSES": [
        "rest_framework.permissions.IsAuthenticated",
    ],
    "DEFAULT_PAGINATION_CLASS": "rest_framework.pagination.PageNumberPagination",
    "PAGE_SIZE": 50,
    "DEFAULT_FILTER_BACKENDS": [
        "rest_framework.filters.SearchFilter",
        "rest_framework.filters.OrderingFilter",
    ],
}

APPEND_SLASH = True


# ---------------------------------------------------------------------------
# Brainbox specific
# ---------------------------------------------------------------------------
# Root directory that holds the source-of-truth knowledge files.
KNOWLEDGE_DATA_ROOT = os.environ.get(
    "BRAINBOX_DATA_ROOT", str(BASE_DIR / "data" / "knowledge")
)

# Master key for the (Phase 5) secret vault. Kept as raw base64 text for now.
BRAINBOX_SECRET_KEY = os.environ.get("BRAINBOX_SECRET_KEY", "")

BRAINBOX_API_KEY_PREFIX = env_list("BRAINBOX_API_KEY_PREFIX", "ck_live_")[0] or "ck_live_"

# Git integration (Phase 2). A single shared token is used for HTTPS remotes;
# per-repository credentials arrive with the Secret Vault (Phase 5).
BRAINBOX_GIT_TOKEN = os.environ.get("BRAINBOX_GIT_TOKEN", "")
BRAINBOX_GIT_AUTHOR_NAME = os.environ.get("BRAINBOX_GIT_AUTHOR_NAME", "Brainbox")
BRAINBOX_GIT_AUTHOR_EMAIL = os.environ.get("BRAINBOX_GIT_AUTHOR_EMAIL", "brainbox@localhost")
BRAINBOX_GIT_COMMAND_TIMEOUT = int(os.environ.get("BRAINBOX_GIT_COMMAND_TIMEOUT", "120"))

# GitHub token used for optional PR creation from Git-backed resources.
BRAINBOX_GITHUB_TOKEN = os.environ.get("BRAINBOX_GITHUB_TOKEN", "")

# Embeddings / chunking / vector search (Phase 3)
BRAINBOX_EMBEDDING_PROVIDER = os.environ.get("BRAINBOX_EMBEDDING_PROVIDER", "deterministic")
BRAINBOX_EMBEDDING_MODEL = os.environ.get("BRAINBOX_EMBEDDING_MODEL", "text-embedding-3-small")
BRAINBOX_EMBEDDING_DIM = int(os.environ.get("BRAINBOX_EMBEDDING_DIM", "256"))
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY", "")
OPENAI_BASE_URL = os.environ.get("OPENAI_BASE_URL", "https://api.openai.com/v1")

BRAINBOX_CHUNK_SIZE = int(os.environ.get("BRAINBOX_CHUNK_SIZE", "1200"))
BRAINBOX_CHUNK_OVERLAP = int(os.environ.get("BRAINBOX_CHUNK_OVERLAP", "150"))
BRAINBOX_AUTO_INDEX = env_bool("BRAINBOX_AUTO_INDEX", True)
BRAINBOX_SEARCH_BACKEND = os.environ.get("BRAINBOX_SEARCH_BACKEND", "simple")

QDRANT_URL = os.environ.get("QDRANT_URL", "").rstrip("/")
QDRANT_API_KEY = os.environ.get("QDRANT_API_KEY", "")
QDRANT_COLLECTION = os.environ.get("QDRANT_COLLECTION", "brainbox_chunks")

# Secret vault (Phase 5)
BRAINBOX_SECRET_SCAN_MODE = os.environ.get("BRAINBOX_SECRET_SCAN_MODE", "off")  # off|warn|reject

# OIDC (Phase 6) - fully optional, disabled unless OIDC_ENABLED is true
OIDC_ENABLED = env_bool("OIDC_ENABLED", False)
OIDC_ISSUER = os.environ.get("OIDC_ISSUER", "").rstrip("/")
OIDC_CLIENT_ID = os.environ.get("OIDC_CLIENT_ID", "")
OIDC_CLIENT_SECRET = os.environ.get("OIDC_CLIENT_SECRET", "")
OIDC_REDIRECT_URI = os.environ.get("OIDC_REDIRECT_URI", "")
OIDC_SCOPES = os.environ.get("OIDC_SCOPES", "openid email profile")
OIDC_AUTHORIZE_ENDPOINT = os.environ.get("OIDC_AUTHORIZE_ENDPOINT", "")
OIDC_TOKEN_ENDPOINT = os.environ.get("OIDC_TOKEN_ENDPOINT", "")
OIDC_USERINFO_ENDPOINT = os.environ.get("OIDC_USERINFO_ENDPOINT", "")
OIDC_AUTO_CREATE_USERS = env_bool("OIDC_AUTO_CREATE_USERS", True)
OIDC_DEFAULT_GROUPS = env_list("OIDC_DEFAULT_GROUPS", "")

# Optional authentication backend wired in only when OIDC is enabled.
if OIDC_ENABLED:
    AUTHENTICATION_BACKENDS = [
        "apps.accounts.oidc.OIDCBackend",
        "django.contrib.auth.backends.ModelBackend",
    ]


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {
        "verbose": {
            "format": "{levelname} {asctime} {name} {message}",
            "style": "{",
        },
    },
    "handlers": {
        "console": {
            "class": "logging.StreamHandler",
            "formatter": "verbose",
        },
    },
    "root": {
        "handlers": ["console"],
        "level": os.environ.get("DJANGO_LOG_LEVEL", "INFO"),
    },
    "loggers": {
        "django.request": {
            "handlers": ["console"],
            "level": "WARNING",
            "propagate": False,
        },
    },
}
