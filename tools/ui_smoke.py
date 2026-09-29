#!/usr/bin/env python
"""Opt-in browser smoke check for the Brainbox web UI.

The Django suite in ``apps/*/tests.py`` asserts the rendered HTML, but it cannot
see what a browser actually does with it: responsive layout, CSS that never
matches, or markup that the HTML parser silently restructures. This script
catches that class of bug. It is deliberately NOT part of `manage.py test` and
not wired into CI - it needs a running server and a real browser.

Setup (WSL/Ubuntu; the system libraries need sudo once):

    pip install -r requirements-browser.txt
    sudo playwright install --with-deps chromium

Run it against a seeded instance:

    python manage.py migrate
    python manage.py runserver 8123 &
    python tools/ui_smoke.py --base http://localhost:8123

Exits non-zero when a check fails, so it is usable as a pre-commit style gate.
Exit code 2 means the environment is not ready (no browser, no server).
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass, field
from pathlib import Path

DEFAULT_BASE = "http://localhost:8123"
DEFAULT_USER = "admin"
DEFAULT_PASS = "demo1234"
SHOT_DIR = Path("ui-smoke-shots")

PAGES = [
    ("dashboard", "/"),
    ("search", "/search/"),
    ("discover", "/discover/"),
    ("calendar", "/calendar/"),
    ("agenda", "/calendar/agenda/"),
    ("api-keys", "/manage/api-keys/"),
    ("audit", "/manage/audit/"),
    ("groups", "/manage/groups/"),
    ("settings", "/manage/settings/"),
]
MOBILE_PAGES = [
    ("dashboard", "/"),
    ("groups", "/manage/groups/"),
    ("settings", "/manage/settings/"),
    ("calendar", "/calendar/"),
]
MOBILE_WIDTH = 390
BAD_PARAMS = [
    "/calendar/?year=abc",
    "/calendar/?month=abc",
    "/calendar/agenda/?days=abc",
    "/calendar/agenda/?days=99999999",
    "/calendar/ical/?days=abc",
    "/search/?mode=bogus&q=x",
]


@dataclass
class Report:
    passed: int = 0
    failures: list[tuple[str, str]] = field(default_factory=list)

    def check(self, name: str, ok: bool, detail: str = "") -> None:
        if ok:
            self.passed += 1
            print(f"PASS  {name}" + (f" :: {detail}" if detail else ""))
        else:
            self.failures.append((name, detail))
            print(f"FAIL  {name}" + (f" :: {detail}" if detail else ""))


def login(page, base: str, user: str, password: str) -> None:
    page.goto(f"{base}/accounts/login/", wait_until="domcontentloaded")
    if "login" not in page.url:
        return
    page.fill('input[name="username"]', user)
    page.fill('input[name="password"]', password)
    page.click('button[type=submit], input[type=submit]')
    page.wait_for_load_state("domcontentloaded")


def audit_dom(page, tag: str, report: Report) -> None:
    """Checks that only make sense against a live layout engine."""
    metrics = page.evaluate(
        """() => {
            const de = document.documentElement;
            const blockish = new Set(['FORM','DIV','UL','TABLE','SECTION','P','H1','H2','H3','PRE']);
            const nestedForms = [...document.querySelectorAll('form')]
                .filter((f) => f.querySelector('form')).length;
            const blockInP = [...document.querySelectorAll('p')]
                .flatMap((p) => [...p.children])
                .filter((c) => blockish.has(c.tagName)).length;
            return {
                scrollWidth: de.scrollWidth,
                clientWidth: de.clientWidth,
                nestedForms,
                blockInP,
                unlabelled: [...document.querySelectorAll(
                    'input:not([type=hidden]):not([type=submit]):not([type=button]),select,textarea')]
                    .filter((el) => {
                        if (el.id && document.querySelector(`label[for="${CSS.escape(el.id)}"]`)) return false;
                        return !(el.closest('label') || el.getAttribute('aria-label') || el.placeholder);
                    }).length,
            };
        }"""
    )
    report.check(
        f"{tag}: no horizontal overflow",
        metrics["scrollWidth"] <= metrics["clientWidth"] + 1,
        f"{metrics['scrollWidth']} vs {metrics['clientWidth']}",
    )
    report.check(f"{tag}: no nested forms", metrics["nestedForms"] == 0)
    report.check(
        f"{tag}: no block element trapped in <p>",
        metrics["blockInP"] == 0,
        "the HTML parser closes <p> early, which moves markup out of its flex row",
    )
    report.check(f"{tag}: all controls labelled", metrics["unlabelled"] == 0, f"{metrics['unlabelled']} unlabelled")


def collect_console(page, tag: str, report: Report) -> None:
    def on_console(message) -> None:
        if message.type == "error":
            report.check(f"{tag}: no console error", False, message.text)

    page.on("console", on_console)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", default=DEFAULT_BASE)
    parser.add_argument("--user", default=DEFAULT_USER)
    parser.add_argument("--password", default=DEFAULT_PASS)
    parser.add_argument("--headed", action="store_true", help="show the browser")
    parser.add_argument("--no-shots", action="store_true", help="skip screenshots")
    args = parser.parse_args()

    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print(
            "playwright is not installed. Run:\n"
            "  pip install -r requirements-browser.txt",
            file=sys.stderr,
        )
        return 2

    if not args.base.startswith(("http://", "https://")):
        print(f"--base must be a URL, got {args.base!r}", file=sys.stderr)
        return 2

    report = Report()
    if not args.no_shots:
        SHOT_DIR.mkdir(exist_ok=True)

    with sync_playwright() as p:
        try:
            browser = p.chromium.launch(headless=not args.headed)
        except Exception as exc:  # noqa: BLE001 - any launch failure is setup debt
            print(f"could not launch chromium: {exc}", file=sys.stderr)
            print(
                "This check needs the browser's system libraries. On WSL/Ubuntu:\n"
                "  sudo playwright install --with-deps chromium",
                file=sys.stderr,
            )
            return 2
        try:
            context = browser.new_context(viewport={"width": 1440, "height": 900})
            page = context.new_page()
            collect_console(page, "desktop", report)
            login(page, args.base, args.user, args.password)

            for name, path in PAGES:
                url = args.base + path
                response = page.goto(url, wait_until="domcontentloaded")
                status = response.status if response else 0
                report.check(f"GET {path}", status == 200, f"HTTP {status}")
                if status != 200:
                    continue
                page.wait_for_timeout(150)
                audit_dom(page, name, report)
                if not args.no_shots:
                    page.screenshot(path=str(SHOT_DIR / f"{name}.png"), full_page=True)

            # The workspace/project pages are not in PAGES because their slugs
            # are data dependent; walk them if the first one exists.
            page.goto(args.base + "/", wait_until="domcontentloaded")
            slug = page.get_attribute('a[href^="/workspaces/"]', "href")
            if slug:
                workspace = args.base + slug
                page.goto(workspace, wait_until="domcontentloaded")
                audit_dom(page, "workspace", report)
                if not args.no_shots:
                    page.screenshot(path=str(SHOT_DIR / "workspace.png"), full_page=True)
                # The ACL panel is included inline here; the owner must not be
                # told they cannot share a resource they administer.
                if page.locator("#access-panel").count():
                    text = page.locator("#access-panel").inner_text()
                    report.check(
                        "workspace: access panel usable by the owner",
                        "nincs írási jogod" not in text,
                        text.splitlines()[0] if text else "",
                    )
                    options = page.locator('#access-panel select[name="subject_id"] option')
                    report.check(
                        "workspace: member picker is populated without a search",
                        options.count() > 1,
                        f"{options.count() - 1} subjects",
                    )

                project = page.get_attribute('a[href*="/workspaces/"][href$="/"]', "href")
                if project and project != slug:
                    page.goto(args.base + project, wait_until="domcontentloaded")
                    audit_dom(page, "project", report)
                    page.goto(args.base + project + "documents/new/", wait_until="domcontentloaded")
                    audit_dom(page, "document-form", report)

            for path in BAD_PARAMS:
                status = page.request.get(args.base + path).status
                report.check(f"robustness {path}", status == 200, f"HTTP {status}")

            mobile = browser.new_context(viewport={"width": MOBILE_WIDTH, "height": 844})
            mpage = mobile.new_page()
            login(mpage, args.base, args.user, args.password)
            for name, path in MOBILE_PAGES:
                mpage.goto(args.base + path, wait_until="domcontentloaded")
                mpage.wait_for_timeout(150)
                audit_dom(mpage, f"mobile-{name}", report)
                if not args.no_shots:
                    mpage.screenshot(
                        path=str(SHOT_DIR / f"mobile-{name}.png"), full_page=True
                    )
        finally:
            browser.close()

    total = report.passed + len(report.failures)
    print(f"\n{report.passed}/{total} checks passed")
    if report.failures:
        print("\nFailures:")
        for name, detail in report.failures:
            print(f"  - {name} :: {detail}")
        return 1
    if not args.no_shots:
        print(f"screenshots in {SHOT_DIR}/")
    return 0


if __name__ == "__main__":
    sys.exit(main())
