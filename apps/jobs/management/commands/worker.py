"""Long-running worker: claims and executes job run rows from the DB queue."""

import logging
import signal
import time

from django.conf import settings
from django.core.management.base import BaseCommand

from apps.jobs import engine, tasks_brainbox  # noqa: F401  (registers handlers)
from apps.jobs.engine import BOOT_ID

logger = logging.getLogger("brainbox.jobs")


class Command(BaseCommand):
    help = "Run the background job worker (claims run rows from the DB queue)."

    def add_arguments(self, parser):
        parser.add_argument("--once", action="store_true", help="Process a single run and exit.")
        parser.add_argument("--interval", type=int, default=None)
        parser.add_argument("--worker-id", default=None)

    def handle(self, *args, **options):
        worker = options.get("worker_id") or BOOT_ID
        interval = options.get("interval") or settings.BRAINBOX_JOB_POLL_SEC
        running = {"value": True}

        def _stop(signum, frame):  # noqa: ARG001
            logger.info("worker received signal %s, draining", signum)
            running["value"] = False

        signal.signal(signal.SIGTERM, _stop)
        signal.signal(signal.SIGINT, _stop)

        logger.info("job worker started (id=%s interval=%ss)", worker, interval)
        try:
            while running["value"]:
                run = engine.run_pending_once(worker)
                if options["once"]:
                    self.stdout.write("processed" if run else "nothing to do")
                    return
                if run is None:
                    time.sleep(interval)
        finally:
            logger.info("job worker stopped")
