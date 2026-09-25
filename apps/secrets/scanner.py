"""Lightweight secret scanner (Phase 2/5, terv.md 44).

Detects likely credentials in uploaded/committed content. Matches are redacted
before they ever reach logs or audit records.
"""

from __future__ import annotations

import re

PATTERNS: list[tuple[str, re.Pattern]] = [
    ("aws_access_key", re.compile(r"AKIA[0-9A-Z]{16}")),
    ("private_key", re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH |PGP )?PRIVATE KEY-----")),
    ("github_token", re.compile(r"gh[pousr]_[A-Za-z0-9]{36,}")),
    ("slack_token", re.compile(r"xox[baprs]-[A-Za-z0-9-]{10,}")),
    ("bearer_token", re.compile(r"(?i)bearer\s+[A-Za-z0-9._\-]{20,}")),
    (
        "generic_credential",
        re.compile(r"(?i)(api[_-]?key|secret|token|password)\s*[:=]\s*['\"]?[A-Za-z0-9_\-]{16,}"),
    ),
]


def _redact(value: str) -> str:
    value = value.strip()
    if len(value) <= 6:
        return "…"
    return f"{value[:4]}…{value[-2:]}"


def scan_text(text: str) -> list[dict]:
    findings: list[dict] = []
    for name, pattern in PATTERNS:
        for match in pattern.finditer(text or ""):
            findings.append(
                {
                    "type": name,
                    "match": _redact(match.group(0)),
                    "index": match.start(),
                }
            )
    return findings


def scan_document_content(content: str) -> list[dict]:
    return scan_text(content)
