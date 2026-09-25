"""Long-running scheduler: fires due jobs (single owner via the DB lease)."""

import logging
import signal
import time

from django.conf import settings
from django.core.management.base import BaseCommand

from apps.jobs import engine, tasks_brainbox  # noqa: F401  (registers handlers)

logger = logging.getLogger("brainbox.jobs")


class Command(BaseCommand):
    help = "Run the job scheduler loop (only the DB-lease owner ticks)."

    def add_arguments(self, parser):
        parser.add_argument("--once", action="store_true", help="Run a single tick and exit.")
        parser.add_argument("--interval", type=int, default=None)

    def handle(self, *args, **options):
        interval = options.get("interval") or settings.BRAINBOX_SCHEDULER_TICK_SEC
        running = {"value": True}

        def _stop(signum, frame):  # noqa: ARG001
            logger.info("scheduler received signal %s, stopping", signum)
            running["value"] = False

        signal.signal(signal.SIGTERM, _stop)
        signal.signal(signal.SIGINT, _stop)

        while running["value"]:
            try:
                with engine.scheduler_lock() as owns:
                    if owns:
                        enqueued = engine.tick()
                        if enqueued:
                            self.stdout.write(f"scheduled {enqueued} run(s)")
                    elif options["once"]:
                        self.stdout.write("scheduler lease held elsewhere")
                        return
            except Exception:  # noqa: BLE001 - a failing tick must not kill the loop
                logger.exception("scheduler tick failed")
            if options["once"]:
                return
            time.sleep(interval)
