"""Background worker entrypoint.

Phase 1 has no queued jobs yet, so this is a supervised idle loop that keeps
the worker container alive and is where Phase 2/3 jobs (git sync, scanning,
chunking, embedding, re-index) will be registered.
"""

import logging
import signal
import time

from django.core.management.base import BaseCommand

logger = logging.getLogger("brainbox.worker")


class Command(BaseCommand):
    help = "Run the background worker loop."

    def add_arguments(self, parser):
        parser.add_argument("--interval", type=int, default=30, help="Idle sleep seconds.")

    def handle(self, *args, **options):
        interval = options["interval"]
        running = {"value": True}

        def _stop(signum, frame):  # noqa: ARG001
            logger.info("worker received signal %s, shutting down", signum)
            running["value"] = False

        signal.signal(signal.SIGTERM, _stop)
        signal.signal(signal.SIGINT, _stop)

        logger.info("worker started (idle loop, interval=%ss)", interval)
        while running["value"]:
            time.sleep(interval)
        logger.info("worker stopped")
