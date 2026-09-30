"""Tests for the self-healing static collection.

The bug these guard against: plain ``collectstatic`` never deletes, so on a
persistent STATIC_ROOT an old build's assets survive every deploy. The instance
served the Phase 3 stylesheet from 2026-09-25 long after the code moved on, and
nothing reported it.
"""

import os
import shutil
import tempfile
from pathlib import Path

from django.core.management import CommandError, call_command
from django.test import SimpleTestCase, TestCase, override_settings

MARKER = "css/app.css"


class _Fixture:
    """Builds a fake static source tree and a matching STATIC_ROOT."""

    def __init__(self, root: Path, *, with_asset: bool = True, prefill_root: bool = True):
        self.root = root
        self.src = root / "src"
        (self.src / "css").mkdir(parents=True)
        if with_asset:
            (self.src / "css" / "app.css").write_text("/* new build */\n", encoding="utf-8")
        (self.src / "other.txt").write_text("x\n", encoding="utf-8")

        self.static_root = root / "staticroot"
        if prefill_root:
            # What a previous deploy left behind.
            (self.static_root / "css").mkdir(parents=True)
            (self.static_root / "css" / "app.css").write_text("/* OLD build */\n", encoding="utf-8")
            (self.static_root / "css" / "gone.css").write_text("/* deleted upstream */\n", encoding="utf-8")

    def settings(self, **extra):
        base = {
            "STATICFILES_DIRS": [str(self.src)],
            "STATIC_ROOT": str(self.static_root),
        }
        base.update(extra)
        return override_settings(**base)


class CollectStaticSafeTests(SimpleTestCase):
    """Uses SimpleTestCase: no DB, only the filesystem."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.fx = _Fixture(self.tmp)

    def test_clears_stale_files(self):
        """The core fix: assets removed upstream must not survive a deploy."""
        with self.fx.settings():
            call_command("collectstatic_safe", "--noinput", verbosity=0)
        self.assertTrue((self.fx.static_root / MARKER).exists())
        self.assertEqual(
            (self.fx.static_root / MARKER).read_text(),
            "/* new build */\n",
        )
        self.assertFalse(
            (self.fx.static_root / "css" / "gone.css").exists(),
            "a file deleted upstream was still in STATIC_ROOT after collecting",
        )

    def test_no_clear_keeps_existing_behaviour(self):
        with self.fx.settings():
            call_command("collectstatic_safe", "--noinput", "--no-clear", verbosity=0)
        self.assertTrue(
            (self.fx.static_root / "css" / "gone.css").exists(),
            "--no-clear must behave like plain collectstatic",
        )

    def test_refuses_to_clear_when_no_source_exists(self):
        """With nothing to collect from, clearing would only delete good files."""
        shutil.rmtree(self.fx.src)
        self.assertFalse(self.fx.src.exists())
        with self.fx.settings():
            call_command("collectstatic_safe", "--noinput", verbosity=0)
        self.assertTrue(
            (self.fx.static_root / MARKER).exists(),
            "refused to clear, so the previous app.css survived",
        )

    def test_raises_when_source_has_no_marker(self):
        """A collect that runs but yields no app.css must fail loudly."""
        (self.fx.src / MARKER).unlink()
        with self.fx.settings():
            with self.assertRaises(CommandError):
                call_command("collectstatic_safe", "--noinput", verbosity=0)

    def test_raises_when_nothing_can_be_collected(self):
        """No source and an empty target -> failure, not a silent success."""
        shutil.rmtree(self.fx.src)
        empty_root = self.tmp / "emptyroot"
        empty_root.mkdir()
        with self.fx.settings(STATICFILES_DIRS=[], STATIC_ROOT=str(empty_root)):
            with self.assertRaises(CommandError):
                call_command("collectstatic_safe", "--noinput", "--no-clear", verbosity=0)


class UnwritableTargetTests(SimpleTestCase):
    """The dangerous case: clear what works, then fail to write.

    A bare Docker tmpfs mount is root-owned, so a container running as uid 1000
    hits this. Clearing first would delete the working assets and then leave the
    UI unstyled, so the target must be probed before anything is removed.
    """

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.fx = _Fixture(self.tmp)

    def test_refuses_to_clear_an_unwritable_target(self):
        root = self.fx.static_root
        root.chmod(0o500)  # readable, not writable
        try:
            if os.access(root, os.W_OK):  # running as root, the test is void
                self.skipTest("running as root; cannot make a dir unwritable")
            with self.fx.settings():
                with self.assertRaises(CommandError) as ctx:
                    call_command("collectstatic_safe", "--noinput", verbosity=0)
        finally:
            root.chmod(0o700)
        self.assertIn("not writable", str(ctx.exception))
        # The working assets are still there.
        self.assertTrue((root / MARKER).exists())

    def test_no_clear_still_attempts_on_an_unwritable_target(self):
        """--no-clear is the escape hatch: no clearing, so nothing is destroyed."""
        root = self.fx.static_root
        root.chmod(0o500)
        try:
            if os.access(root, os.W_OK):
                self.skipTest("running as root; cannot make a dir unwritable")
            with self.fx.settings():
                with self.assertRaises(CommandError):
                    call_command("collectstatic_safe", "--noinput", "--no-clear", verbosity=0)
        finally:
            root.chmod(0o700)
        self.assertTrue((root / MARKER).exists())


class ReadyzStaticCheckTests(TestCase):
    """A missing marker must be reported without failing readiness.

    ``TestCase``, not ``SimpleTestCase``: ``/readyz`` opens a database cursor, and
    SimpleTestCase forbids that, which would show up as ``database: error`` and
    mask what these tests are actually about.
    """

    def _readyz(self):
        from django.test import Client

        return Client().get("/readyz")

    def test_readyz_reports_static_ok(self):
        tmp = Path(tempfile.mkdtemp())
        fx = _Fixture(tmp)
        with fx.settings(), override_settings(KNOWLEDGE_DATA_ROOT=str(tmp / "data")):
            call_command("collectstatic_safe", "--noinput", verbosity=0)
            body = self._readyz().json()
        self.assertEqual(body.get("static"), "ok")

    def test_readyz_reports_missing_static_without_going_unready(self):
        """An unstyled UI is a correctness problem, not an availability one.

        Failing readiness here would pull a working instance out of rotation over
        cosmetics, and would break every environment where collectstatic has not
        run yet (CI, a bare dev checkout).
        """
        tmp = Path(tempfile.mkdtemp())
        fx = _Fixture(tmp)
        empty = tmp / "emptystatic"
        empty.mkdir()
        with fx.settings(STATIC_ROOT=str(empty)), override_settings(
            KNOWLEDGE_DATA_ROOT=str(tmp / "data")
        ):
            response = self._readyz()
            body = response.json()
        self.assertEqual(body.get("static"), "missing")
        self.assertEqual(body.get("static_dir"), str(empty))
        self.assertEqual(response.status_code, 200, "a missing asset must not fail readiness")
        self.assertEqual(body.get("status"), "ok")