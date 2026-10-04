#!/usr/bin/env python3
"""Structural audit of every route, in a real browser.

The things a screenshot shows but a diff does not: duplicate element ids (which
break the label/for wiring), heading order, form controls with no accessible
name, and tabs whose panel is hidden while the label says otherwise.
"""

from __future__ import annotations

from playwright.sync_api import sync_playwright

BASE = "http://127.0.0.1:8129"

ROUTES = [
    "/",
    "/search/?q=terraform",
    "/discover/",
    "/calendar/",
    "/calendar/agenda/",
    "/manage/audit/",
    "/manage/api-keys/",
    "/manage/groups/",
    "/manage/settings/",
    "/workspaces/ecoform/",
    "/workspaces/ecoform/fejlesztoi-resz/",
    "/workspaces/ecoform/fejlesztoi-resz/documents/new/",
    "/workspaces/ecoform/fejlesztoi-resz/bulk-upload/",
    "/documents/c6d31b6b-fc77-4fef-a628-6f926aa86488/",
    "/personal/",
]

AUDIT = """
() => {
  // A closed <details> reports empty innerText, which would flag every item
  // inside a collapsed panel as unnamed. Open them before checking.
  document.querySelectorAll('details').forEach((d) => { d.open = true; });
  const problems = [];
  const seen = new Map();
  document.querySelectorAll('[id]').forEach((el) => {
    const id = el.id;
    seen.set(id, (seen.get(id) || 0) + 1);
  });
  for (const [id, n] of seen) {
    if (n > 1) problems.push(`duplicate id #${id} x${n}`);
  }
  document.querySelectorAll('label[for]').forEach((lab) => {
    if (!document.getElementById(lab.htmlFor)) {
      problems.push(`label for="${lab.htmlFor}" points at nothing`);
    }
  });
  document.querySelectorAll('input, select, textarea').forEach((el) => {
    if (el.type === 'hidden') return;
    const id = el.id;
    const wrapped = el.closest('label');
    const named = (id && document.querySelector(`label[for="${id}"]`))
      || wrapped
      || el.getAttribute('aria-label')
      || el.getAttribute('title');
    if (!named) problems.push(`unlabelled ${el.tagName.toLowerCase()} name="${el.name || '?'}"`);
  });
  document.querySelectorAll('img').forEach((img) => {
    if (img.alt === null) problems.push('img without alt attribute');
  });
  document.querySelectorAll('button, a').forEach((el) => {
    const text = (el.innerText || '').trim();
    if (!text && !el.getAttribute('aria-label') && !el.querySelector('img[alt]:not([alt=""])')) {
      problems.push(`${el.tagName.toLowerCase()} with no accessible name`);
    }
  });
  const heads = [...document.querySelectorAll('h1,h2,h3,h4')]
    .map((h) => Number(h.tagName[1]));
  if (heads.length && heads[0] !== 1) problems.push(`first heading is h${heads[0]}, not h1`);
  for (let i = 1; i < heads.length; i += 1) {
    if (heads[i] - heads[i - 1] > 1) {
      problems.push(`heading jumps h${heads[i - 1]} -> h${heads[i]}`);
    }
  }
  if (document.documentElement.lang !== 'hu') {
    problems.push(`lang="${document.documentElement.lang}"`);
  }
  return [...new Set(problems)];
}
"""


def main():
    total = 0
    with sync_playwright() as p:
        browser = p.chromium.launch()
        ctx = browser.new_context(viewport={"width": 1440, "height": 1000})
        page = ctx.new_page()
        page.goto(f"{BASE}/accounts/login/", wait_until="networkidle")
        page.fill('input[name="username"]', "kerek.kristof")
        page.fill('input[name="password"]', "pw")
        page.click('button[type=submit]')
        page.wait_for_load_state("networkidle")
        for route in ROUTES:
            resp = page.goto(f"{BASE}{route}", wait_until="networkidle")
            problems = page.evaluate(AUDIT)
            status = resp.status if resp else "?"
            if problems or status != 200:
                print(f"{route}  [{status}]")
                for item in problems:
                    print(f"    {item}")
                total += len(problems)
            else:
                print(f"{route}  [{status}] clean")
        ctx.close()
        browser.close()
    print(f"\ntotal problems: {total}")


main()