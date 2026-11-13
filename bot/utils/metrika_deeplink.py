"""Parse Metrika ClientId from Telegram deep-link start payloads.

Telegram `start` allows only [A-Za-z0-9_-] and max 64 chars.
We append ClientId as: `{base}_c{cid}` or `{base}_src_{source}_c{cid}`.
"""

from __future__ import annotations

import re

_CID_RE = re.compile(
    r"^(?P<base>.+?)(?:_src_(?P<source>[A-Za-z0-9-]+))?_c(?P<cid>\d+)$",
    re.IGNORECASE,
)


def parse_start_payload(raw: str | None) -> tuple[str, str, str]:
    """Return (base_payload, cid, source).

    source is empty when there is no cid suffix.
    When only `_c{cid}` is present, source defaults to ``site``.
    """
    value = (raw or "").strip()
    if not value:
        return "", "", ""

    match = _CID_RE.match(value)
    if not match:
        return value, "", ""

    base = (match.group("base") or "").strip()
    cid = (match.group("cid") or "").strip()
    source = (match.group("source") or "").strip() or "site"
    return base or value, cid, source


def build_start_payload(base: str, *, client_id: str = "", source: str = "site") -> str:
    """Build a Telegram start payload with optional Metrika ClientId."""
    base_value = (base or "").strip()
    cid = re.sub(r"\D", "", (client_id or "").strip())
    if not base_value:
        return ""
    if not cid:
        return base_value

    source_value = re.sub(r"[^A-Za-z0-9-]", "", (source or "site").strip()) or "site"
    if source_value == "site":
        candidate = f"{base_value}_c{cid}"
    else:
        candidate = f"{base_value}_src_{source_value}_c{cid}"

    if len(candidate) <= 64:
        return candidate
    # Prefer keeping cid; drop custom source if too long.
    short = f"{base_value}_c{cid}"
    return short if len(short) <= 64 else base_value
