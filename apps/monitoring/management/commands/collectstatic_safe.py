"""Collect static assets, replacing the target directory wholesale."""

from __future__ import annotations

import os
from pathlib import Path

from django.conf import settings
from django.contrib.staticfiles.management.commands.collectstatic import (
    Command as CollectStaticCommand,
)
from django.core.management import CommandError


class Command(CollectStaticCommand):
    """``collectstatic`` that cannot leave a stale or empty STATIC_ROOT behind.

    Plain ``collectstatic`` never deletes, so on a *persistent* STATIC_ROOT the
    assets of an old build survive every deploy and keep being served. That is
    how this instance ended up answering with the Phase 3 stylesheet from
    2026-09-25 long after the code had moved on: the app looked alive, only the
    CSS was stale, and nothing reported it.

    So: check, clear, then verify. ``--clear`` is destructive, so two situations
    are refused outright rather than attempted:

    * the target is not writable -- clearing would destroy the currently working
      assets and then fail to put new ones there, leaving the UI unstyled. This
      is not hypothetical: a bare Docker ``tmpfs`` mount is root-owned, so an
      ``app``-owned container cannot write it.
    * no source directory exists -- clearing would only delete what works.

    There is deliberately no "restore the old assets" retry: once ``--clear``
    succeeds the previous files are gone, so such a retry could not put them
    back. Refusing up front is the only honest protection.
    """

    help = "Collect static files, clearing stale ones and verifying the result."

    #: Relative path that must exist afterwards. app.css is referenced by every
    #: page, so its absence means the UI is unstyled.
    default_marker = "css/app.css"

    def add_arguments(self, parser):
        super().add_arguments(parser)
        parser.add_argument(
            "--no-clear",
            action="store_true",
            help="Keep the standard collectstatic behaviour (never clear first).",
        )
        parser.add_argument(
            "--marker",
            default=self.default_marker,
            help="Relative path under STATIC_ROOT that must exist after collecting.",
        )

    # -- helpers -------------------------------------------------------------
    def _root(self) -> Path:
        return Path(settings.STATIC_ROOT)

    def _sources(self) -> list[Path]:
        return [Path(p) for p in getattr(settings, "STATICFILES_DIRS", []) or []]

    def _count(self, root: Path) -> int:
        if not root.is_dir():
            return 0
        return sum(1 for p in root.rglob("*") if p.is_file())

    @staticmethod
    def _writable_target(root: Path) -> Path | None:
        """Nearest existing ancestor of ``root``, if this process may write it."""
        probe = root
        while not probe.exists() and probe != probe.parent:
            probe = probe.parent
        return probe if probe.is_dir() and os.access(probe, os.W_OK) else None

    def _diagnose(self, root: Path, sources: list[Path], problem: str) -> str:
        uid = f"{os.getuid()}:{os.getgid()}"
        return "\n".join(
            [
                problem,
                f"  target  : {root} (want uid {uid})",
                f"  sources : {', '.join(str(p) for p in sources) or '(none)'}",
                "  fix     : mount /app/staticfiles as a tmpfs owned by the "
                f"container uid ({uid}), e.g.",
                f"           tmpfs: [\"/app/staticfiles:uid={os.getuid()},"
                f"gid={os.getgid()},mode=0755\"]",
            ]
        )

    # -- command -------------------------------------------------------------
    def handle(self, *args, **options):
        root = self._root()
        sources = self._sources()
        marker = root / options["marker"]

        want_clear = not options["no_clear"]

        if want_clear:
            writable = self._writable_target(root)
            if writable is None:
                raise CommandError(
                    self._diagnose(
                        root, sources, f"{root} is not writable; refusing to clear"
                    )
                )
            if not any(p.exists() for p in sources):
                self.stdout.write(
                    f"clearing: no (no source directory exists among "
                    f"{[str(p) for p in sources]}) -- refusing to empty {root}"
                )
                want_clear = False

        options["clear"] = want_clear
        self.stdout.write(f"clearing: {'yes' if want_clear else 'no'} ({root})")

        try:
            super().handle(*args, **options)
        except OSError as exc:
            # Django's collectstatic lets filesystem errors escape raw, which is
            # a bare PermissionError traceback with no hint about the fix.
            raise CommandError(
                self._diagnose(
                    root, sources, f"static collection failed: {exc.__class__.__name__}: {exc}"
                )
            ) from exc

        collected = self._count(root)
        self.stdout.write(f"collected: {collected} file(s) in {root}")

        if collected and marker.exists():
            return

        raise CommandError(
            self._diagnose(
                root, sources, f"static collection did not produce {marker}"
            )
        )