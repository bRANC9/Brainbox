#!/usr/bin/env python3
"""Dump what a user actually sees on each page: the visible text, in order.

No pixels needed - this is the information structure, which is where 'muddled'
lives: repeated labels, unclear order, missing affordances.
"""

from __future__ import annotations

import html
import re
import urllib.error
import urllib.parse
import urllib.request
from http.cookiejar import CookieJar

BASE = "http://127.0.0.1:8129"
PAGES = [
    ("dashboard", "/"),
    ("workspace", "/workspaces/ecoform/"),
    ("project", "/workspaces/ecoform/fejlesztoi-resz/"),
    ("brainbox", "/workspaces/brainbox/"),
]
USERS = [("kerek.kristof", "pw"), ("dev.one", "pw"), ("outsider", "pw")]

TAG = re.compile(r"<[^>]+>")
SKIP = re.compile(r"<(script|style)\b.*?</\1>", re.S | re.I)


def visible(markup: str) -> str:
    body = SKIP.sub(" ", markup)
    main = re.search(r"<main\b.*?</main>", body, re.S | re.I)
    body = main.group(0) if main else body
    body = re.sub(r"<(h[1-6])[^>]*>", r"\n\n## ", body, flags=re.I)
    body = re.sub(r"</(h[1-6])>", "\n", body, flags=re.I)
    body = re.sub(r"<li[^>]*>", "\n- ", body, flags=re.I)
    body = re.sub(r"<(tr|div|p|br)\b[^>]*>", "\n", body, flags=re.I)
    body = re.sub(r"<(td|th)[^>]*>", " | ", body, flags=re.I)
    text = html.unescape(TAG.sub(" ", body))
    lines = [re.sub(r"\s+", " ", line).strip() for line in text.splitlines()]
    return "\n".join(line for line in lines if line)


def session(username, password) -> urllib.request.Request:
    jar = CookieJar()
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
    data = urllib.parse.urlencode(
        {"username": username, "password": password, "csrfmiddlewaretoken": token(opener, BASE)}
    ).encode()
    req = urllib.request.Request(f"{BASE}/accounts/login/", data=data)
    req.add_header("Referer", f"{BASE}/accounts/login/")
    opener.open(req)
    return opener


def token(opener, url) -> str:
    body = opener.open(url).read().decode()
    found = re.search(r'name="csrfmiddlewaretoken" value="([^"]+)"', body)
    return found.group(1) if found else ""


for username, password in USERS:
    opener = session(username, password)
    print("=" * 78)
    print(f"### {username}")
    print("=" * 78)
    for label, path in PAGES:
        try:
            markup = opener.open(f"{BASE}{path}").read().decode()
        except urllib.error.HTTPError as exc:
            print("\n--- %s %s\n(status %s)" % (label, path, exc.code))
            continue
        if "Not Found" in markup or "<h1>Not Found" in markup:
            print(f"\n--- {label} {path}\n(404)")
            continue
        print(f"\n--- {label} {path}")
        print(visible(markup))
    print()