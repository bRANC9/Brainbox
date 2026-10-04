#!/usr/bin/env python3
"""Sweep the templates for the pattern that keeps paying off: English leftovers
and empty-state handling. Cheap, and it has found more than reading did.
"""

import pathlib
import re

# Words that only appear in user-facing English in a Hungarian product.
ENGLISH = re.compile(
    r"\b("
    r"Search|Query|Mode|Updated|Saved|Save|Delete|Remove|Edit|Add|Create|"
    r"Cancel|Close|Open|Back|Next|Previous|Submit|Filter|Sort|Loading|"
    r"None|True|False|Yes|No|Settings|Dashboard|Overview|Details|"
    r"Description|Title|Comment|Status|Owner|Shared|Public|Private|"
    r"Upload|Download|Preview|Print|Refresh|Retry|Error|Warning|Success|"
    r"result\(s\)|not found|no results|forgot|required|invalid"
    r")\b"
)

# A template string node: {{ ... }} or {% ... %} with a quoted literal inside.
STRINGS = re.compile(r"\{\{[^}]*\}\}|\{%[^%]*%\}")

hits = []
for path in sorted(pathlib.Path("templates").glob("*.html")):
    for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if "{% comment" in line or line.strip().startswith("{% comment"):
            continue
        # Only literal text nodes and quoted literals in tags count as UI text.
        chunks = []
        chunks += [c for c in re.findall(r">([^<>{}]+)<", line)]
        chunks += re.findall(r"[\"']([^\"']{2,})[\"']", line)
        for chunk in chunks:
            for word in ENGLISH.findall(chunk):
                hits.append((path.name, n, word, chunk.strip()[:70]))

print(f"=== English in user-facing template text ({len(hits)} hits) ===")
for name, n, word, ctx in hits:
    print(f"  {name}:{n}  [{word}]  {ctx}")

print()
print("=== suspicious empty-state literals ===")
for path in sorted(pathlib.Path("templates").glob("*.html")):
    for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        for pattern in (r">\s*None\s*<", r">\s*\[\]\s*<", r">\s*\{\}\s*<", r">\s*-\s*<"):
            if re.search(pattern, line):
                print(f"  {path.name}:{n}  {line.strip()[:80]}")

print()
print("=== inputs without a label, in the same <form> block ===")
for path in sorted(pathlib.Path("templates").glob("*.html")):
    text = path.read_text(encoding="utf-8")
    labelled = set(re.findall(r'<label[^>]*for="([^"]+)"', text))
    labelled |= set(re.findall(r"<label[^>]*>\s*[^<]*<input", text))
    inputs = re.findall(r"<input\b[^>]*>", text)
    for tag in inputs:
        m = re.search(r'id="([^"]+)"', tag)
        if m and m.group(1) in labelled:
            continue
        name = re.search(r'name="([^"]+)"', tag)
        typ = re.search(r'type="([^"]+)"', tag)
        t = typ.group(1) if typ else "text"
        if t in {"hidden", "checkbox", "radio", "submit"}:
            continue
        if "aria-label" in tag or "placeholder" in tag:
            continue
        print(f"  {path.name}: <input {name.group(1) if name else '?'}>")