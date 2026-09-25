# syntax=docker/dockerfile:1

# ---------------------------------------------------------------------------
# Brainbox - Knowledge Platform
# Single image used by both web and worker roles (command differs).
# Built by GitHub Actions and pushed to ghcr.io/branc9/brainbox.
# TrueNAS only pulls this image, it never builds it.
# ---------------------------------------------------------------------------

FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# git + ca-certificates are needed for the (Phase 2) Git integration.
RUN apt-get update \
    && apt-get install -y --no-install-recommends git ca-certificates \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

RUN useradd --create-home --uid 1000 app \
    && chmod +x /app/entrypoint.sh \
    && mkdir -p /data/knowledge /app/staticfiles \
    && chown -R app:app /app /data

USER app

EXPOSE 8000

ENTRYPOINT ["/app/entrypoint.sh"]
CMD ["gunicorn", "config.wsgi:application", \
     "--bind", "0.0.0.0:8000", \
     "--workers", "3", \
     "--timeout", "120", \
     "--access-logfile", "-", \
     "--error-logfile", "-"]
