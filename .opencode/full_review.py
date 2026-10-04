#!/usr/bin/env python3
"""Every page, at mobile and FHD, in both themes.

The last round was asked to look at the whole interface again, so this walks all
of it rather than the handful that had shown problems. It records a screenshot per
page/width/theme and flags anything structural, so a regression anywhere shows up
even if I do not read all 120 images by eye.
"""

from __future__ import annotations

from pathlib import Path

from playwright.sync_api import sync_playwright

BASE = "http://127.0.0.1:8129"
OUT = Path("/tmp/bb-ui/full")
OUT.mkdir(parents=True, exist_ok=True)

SIZES = {"mobile": (390, 844), "fhd": (1920, 1080)}

ROUTES = [
    ("dashboard", "/"),
    ("search", "/search/?q=terraform"),
    ("search-empty", "/search/?q=zzzznothing"),
    ("discover", "/discover/"),
    ("calendar", "/calendar/"),
    ("agenda", "/calendar/agenda/"),
    ("audit", "/manage/audit/"),
    ("apikeys", "/manage/api-keys/"),
    ("groups", "/manage/groups/"),
    ("settings", "/manage/settings/"),
    ("workspace", "/workspaces/ecoform/"),
    ("project", "/workspaces/ecoform/fejlesztoi-resz/"),
    ("docnew", "/workspaces/ecoform/fejlesztoi-resz/documents/new/"),
    ("bulk", "/workspaces/ecoform/fejlesztoi-resz/bulk-upload/"),
    ("files", "/workspaces/ecoform/files/"),
    ("acl", "/resources/0b992e98-b4b9-4ebf-9af6-e84f8e3a56e9/permissions/"),
    ("acl-owner", "/resources/0b992e98-b4b9-4ebf-9af6-e84f8e3a56e9/permissions/?tab=owner"),
    ("acl-security", "/resources/0b992e98-b4b9-4ebf-9af6-e84f8e3a56e9/permissions/?tab=security"),
    ("doc", "/documents/c6d31b6b-fc77-4fef-a628-6f926aa86488/"),
    ("personal", "/personal/"),
    ("login", "/accounts/login/"),
]

CHECK = """
() => {
  const problems = [];
  document.querySelectorAll('details').forEach((d) => { d.open = true; });
  if (document.documentElement.scrollWidth > document.documentElement.clientWidth + 1) {
    problems.push(`overflow ${document.documentElement.scrollWidth}>${document.documentElement.clientWidth}`);
  }
  document.querySelectorAll('[id]').forEach((el) => {
    if (el.tagName === 'DETAILS' || el.classList.contains('tab-radio')) return;
  });
  document.querySelectorAll('label[for]').forEach((lab) => {
    if (!document.getElementById(lab.htmlFor)) problems.push(`dangling label for="${lab.htmlFor}"`);
  });
  document.querySelectorAll('input, select, textarea').forEach((el) => {
    if (el.type === 'hidden') return;
    const id = el.id;
    if ((id && document.querySelector(`label[for="${id}"]`)) || el.closest('label')
        || el.getAttribute('aria-label') || el.getAttribute('placeholder')) return;
    problems.push(`unlabelled ${el.name || el.tagName}`);
  });
  document.querySelectorAll('button, a').forEach((el) => {
    if ((el.innerText || '').trim()) return;
    if (el.getAttribute('aria-label') || el.querySelector('img[alt]:not([alt=""])')) return;
    problems.push(`nameless ${el.tagName.toLowerCase()}`);
  });
  const h = [...document.querySelectorAll('h1,h2,h3,h4')].map((x) => Number(x.tagName[1]));
  if (h.length && h[0] !== 1) problems.push(`first heading h${h[0]}`);
  for (let i = 1; i < h.length; i += 1) if (h[i] - h[i - 1] > 1) problems.push(`h${h[i - 1]}->h${h[i]}`);
  const body = getComputedStyle(document.body);
  return { problems: [...new Set(problems)], bg: body.backgroundColor, fg: body.color };
}
"""


def main():
    total = 0
    shots = 0
    with sync_playwright() as p:
        browser = p.chromium.launch()
        for size, (w, h) in SIZES.items():
            for theme in ("dark", "light"):
                ctx = browser.new_context(viewport={"width": w, "height": h})
                page = ctx.new_page()
                page.goto(f"{BASE}/accounts/login/", wait_until="networkidle")
                page.fill('input[name="username"]', "kerek.kristof")
                page.fill('input[name="password"]', "pw")
                page.click('button[type=submit]')
                page.wait_for_load_state("networkidle")
                page.evaluate(f"(t) => document.documentElement.dataset.theme = '{theme}'", theme)
                for label, path in ROUTES:
                    resp = page.goto(f"{BASE}{path}", wait_until="networkidle")
                    page.evaluate(f"(t) => document.documentElement.dataset.theme = '{theme}'", theme)
                    page.wait_for_timeout(120)
                    name = f"{theme}-{size}-{label}"
                    # Screenshot BEFORE the audit opens every <details>, otherwise
                    # every shot has the admin menu hanging open.
                    page.screenshot(path=str(OUT / f"{name}.png"))
                    shots += 1
                    out = page.evaluate(CHECK)
                    status = resp.status if resp else "?"
                    if out["problems"] or status != 200:
                        total += len(out["problems"]) + (0 if status == 200 else 1)
                        print(f"{name:34s} [{status}] {out['problems'] or ''}"
                              + ("" if status == 200 else " <-- non-200"))
                ctx.close()
        browser.close()
    print(f"\n{shots} screenshots, {total} problems")


main()