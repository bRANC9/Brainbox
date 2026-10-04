#!/usr/bin/env python3
"""Can a user with only a deep-folder grant reach it by clicking?"""
from pathlib import Path

from playwright.sync_api import sync_playwright

BASE = "http://127.0.0.1:8129"
OUT = Path("/tmp/bb-ui/full")
OUT.mkdir(parents=True, exist_ok=True)


def show(page, title):
    print(f"\n--- {title}")
    rows = page.eval_on_selector_all(
        ".tree-row",
        "els => els.map(e => ({"
        " name: e.innerText.trim().split('\\n')[0],"
        " readable: e.dataset.canRead,"
        " links: e.querySelectorAll('a').length }))",
    )
    if not rows:
        print("  (empty tree)")
    for row in rows:
        print(f"    {row['name'][:44]:46s} readable={row['readable']} links={row['links']}")


with sync_playwright() as p:
    b = p.chromium.launch()
    page = b.new_context(viewport={"width": 1440, "height": 1300}).new_page()
    errors = []
    page.on("pageerror", lambda e, acc=errors: acc.append(str(e)))
    page.goto(f"{BASE}/accounts/login/", wait_until="networkidle")
    page.fill('input[name="username"]', "outsider")
    page.fill('input[name="password"]', "pw")
    page.click('button[type=submit]')
    page.wait_for_load_state("networkidle")

    resp = page.goto(f"{BASE}/workspaces/ecoform/", wait_until="networkidle")
    page.wait_for_timeout(250)
    show(page, f"workspace page [HTTP {resp.status}] - only a deep grant, as 'outsider'")

    # Can it be clicked through? Follow the trail node's link if there is one.
    link = page.query_selector('.tree-row[data-type="dir"] a')
    print(f"    trail node is clickable: {bool(link)}")
    if link:
        page.click('.tree-row[data-type="dir"] a')
        page.wait_for_load_state("networkidle")
        page.wait_for_timeout(250)
        print(f"    -> landed on {page.url.replace(BASE, '')}")
        show(page, "reached by clicking")
    page.screenshot(path=str(OUT / "gap-closed.png"), full_page=True)
    print("\n  JS errors:", errors or "none")
    b.close()