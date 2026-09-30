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
# collectstatic_safe clears stale assets first (plain collectstatic never does,
# so a persistent STATIC_ROOT keeps serving an old build's CSS forever) and
# falls back to a plain collect if that would leave the dir empty. It also logs
# what landed where, which is what makes a bad static dir diagnosable.
#
# Still best-effort: a permission problem on a bind-mounted STATIC_ROOT must not
# take the whole app down. /readyz reports the static dir separately, so a
# broken one is visible without the container failing to boot. Migrations above
# stay fatal on purpose.
if python manage.py collectstatic_safe --noinput; then
    :
else
    echo "[brainbox] WARNING: static collection failed (continuing)." >&2
    echo "[brainbox] The UI may be unstyled; /readyz reports the static dir." >&2
fi

if [ -n "${BRAINBOX_ADMIN_USERNAME:-}" ] && [ -n "${BRAINBOX_ADMIN_PASSWORD:-}" ]; then
    echo "[brainbox] ensuring bootstrap admin user '${BRAINBOX_ADMIN_USERNAME}'..."
    python manage.py bootstrap \
        --username "${BRAINBOX_ADMIN_USERNAME}" \
        --email "${BRAINBOX_ADMIN_EMAIL:-}" \
        --password "${BRAINBOX_ADMIN_PASSWORD}"
fi

exec "$@"
