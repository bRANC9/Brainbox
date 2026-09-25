#!/bin/sh
# Brainbox container entrypoint.
# Migrates, collects static files, optionally bootstraps an admin, then execs
# the container command (gunicorn for web, worker command for the worker role).
set -e

echo "[brainbox] waiting for database and applying migrations..."
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

echo "[brainbox] collecting static files..."
python manage.py collectstatic --noinput

if [ -n "${BRAINBOX_ADMIN_USERNAME:-}" ] && [ -n "${BRAINBOX_ADMIN_PASSWORD:-}" ]; then
    echo "[brainbox] ensuring bootstrap admin user '${BRAINBOX_ADMIN_USERNAME}'..."
    python manage.py bootstrap \
        --username "${BRAINBOX_ADMIN_USERNAME}" \
        --email "${BRAINBOX_ADMIN_EMAIL:-}" \
        --password "${BRAINBOX_ADMIN_PASSWORD}"
fi

exec "$@"
