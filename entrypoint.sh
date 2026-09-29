#!/bin/sh
# Brainbox container entrypoint.
# Migrates, collects static files, optionally bootstraps an admin, then execs
# the container command (gunicorn for web, worker command for the worker role).
set -e

echo "[brainbox] waiting for database and applying migrations..."
if [ "${BRAINBOX_RUN_MIGRATIONS:-1}" = "0" ]; then
    echo "[brainbox] BRAINBOX_RUN_MIGRATIONS=0, skipping migrate (another role owns it)."
else
    attempt=0
    until python manage.py migrate --noinput; do
        attempt=$((attempt + 1))
        if [ "$attempt" -ge 15 ]; then
            echo "[brainbox] database did not become ready in time, giving up." >&2
            exit 1
        fi
        echo "[brainbox] database not ready yet (attempt $attempt/15), retrying in 3s..."
        sleep 3
    done
fi

echo "[brainbox] collecting static files..."
# Static collection is best-effort: a permission problem on a bind-mounted
# STATIC_ROOT must not take the whole app down (the service still serves; only
# the styling may be stale). Migrations above stay fatal on purpose.
if python manage.py collectstatic --noinput; then
    :
else
    echo "[brainbox] WARNING: collectstatic failed (continuing)." >&2
    echo "[brainbox] If the UI looks unstyled, fix the owner of the static dir:" >&2
    echo "[brainbox]   chown -R $(id -u):$(id -g) <host-path-of-/app/staticfiles>" >&2
    echo "[brainbox] or mount /app/staticfiles as a tmpfs in the compose file." >&2
fi

if [ -n "${BRAINBOX_ADMIN_USERNAME:-}" ] && [ -n "${BRAINBOX_ADMIN_PASSWORD:-}" ]; then
    echo "[brainbox] ensuring bootstrap admin user '${BRAINBOX_ADMIN_USERNAME}'..."
    python manage.py bootstrap \
        --username "${BRAINBOX_ADMIN_USERNAME}" \
        --email "${BRAINBOX_ADMIN_EMAIL:-}" \
        --password "${BRAINBOX_ADMIN_PASSWORD}"
fi

exec "$@"
