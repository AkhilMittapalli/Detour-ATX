"""Load secrets from a project-local .env file.

Why a file rather than a shell variable: a key set with `set`/`export` lives
only in that one shell, so a key you set in your terminal is invisible to any
other process. `setx` fixes that by writing it into the Windows registry,
where it is then set for everything you run forever and awkward to rotate.
A gitignored file next to the project is scoped, greppable, and deleted by
deleting it.

Real environment variables always win, so CI and container secrets are not
overridden by a stray file.
"""

from __future__ import annotations

import os
from pathlib import Path

ENV_PATH = Path(__file__).resolve().parent.parent / ".env"

# Only keys this project actually uses. A .env should not be able to set
# PATH or anything else with reach beyond here.
ALLOWED = {
    "GEMINI_API_KEY",
    "GEMINI_MODEL",
    "ANTHROPIC_API_KEY",
    "DETOUR_MODEL",
    "SOCRATA_APP_TOKEN",
    "DETOUR_LEDGER",
    "SMTP_HOST",
    "SMTP_PORT",
    "SMTP_USER",
    "SMTP_PASSWORD",
    "SMTP_FROM",
    "SMTP_TO",
    "DETOUR_WEBHOOK_URL",
}

_loaded = False


def load(path: Path | None = None) -> int:
    """Read .env into os.environ. Returns how many values were set."""
    global _loaded
    target = path or ENV_PATH
    if not target.exists():
        _loaded = True
        return 0

    applied = 0
    # utf-8-sig, not utf-8: PowerShell's `Out-File -Encoding utf8` and
    # `Set-Content -Encoding utf8` both write a BOM on Windows PowerShell 5.1.
    # Read as plain utf-8 and the first key becomes "﻿GEMINI_API_KEY",
    # which silently matches nothing and looks exactly like an unset key.
    for raw in target.read_text(encoding="utf-8-sig").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue

        name, _, value = line.partition("=")
        name = name.strip().removeprefix("export ").strip()
        value = value.strip().strip('"').strip("'")

        if name not in ALLOWED or not value:
            continue
        if os.environ.get(name):      # a real env var outranks the file
            continue
        os.environ[name] = value
        applied += 1

    _loaded = True
    return applied


def ensure_loaded() -> None:
    if not _loaded:
        load()


def describe() -> str:
    """Human-readable account of what is configured, without leaking values."""
    ensure_loaded()
    lines = [f".env: {ENV_PATH}", f"      {'found' if ENV_PATH.exists() else 'not present'}", ""]
    for name in sorted(ALLOWED):
        value = os.environ.get(name)
        if not value:
            lines.append(f"  {name:<20} not set")
        elif name.endswith(("_KEY", "_TOKEN")):
            lines.append(f"  {name:<20} set ({len(value)} chars, ends {value[-4:]})")
        else:
            lines.append(f"  {name:<20} {value}")
    return "\n".join(lines)
