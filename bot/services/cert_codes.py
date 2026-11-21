"""Разбор стартовой ссылки и номера бумажного сертификата."""

from __future__ import annotations

import re

_CODE_RE = re.compile(r"[A-Z0-9]+(?:-[A-Z0-9]+)*")
_REF_SAFE = re.compile(r"[^A-Za-z0-9:._-]+")


def normalize_certificate_code(raw: str | None) -> str | None:
    """Верхний регистр, без пробелов. Буквы, цифры и дефис."""
    text = re.sub(r"\s+", "", (raw or "").strip().upper())
    if not text or len(text) > 40:
        return None
    if not _CODE_RE.fullmatch(text):
        return None
    return text


def is_preview_code(code: str | None) -> bool:
    return (code or "").upper().startswith("TEST")


def parse_certificate_start(raw: str | None) -> tuple[str, str]:
    """Вернуть (handoff_token, code).

    `msscert_TOKEN` — токен страницы.
    `msscert_num_MSS-000001` — номер уже вписан.
    Пустая пара — ссылка без параметра, нужен ручной ввод.
    """
    value = (raw or "").strip()
    low = value.casefold()
    num_prefix = "msscert_num_"
    token_prefix = "msscert_"
    if low.startswith(num_prefix):
        return "", value[len(num_prefix) :]
    if low.startswith(token_prefix):
        return value[len(token_prefix) :], ""
    if low in {"msscert", "mss_certificate", "certificate"}:
        return "", ""
    return "", ""


def looks_like_certificate_start(raw: str | None) -> bool:
    low = (raw or "").strip().casefold()
    return low.startswith("msscert") or low in {"mss_certificate", "certificate"}


def safe_ref(value: str, *, limit: int = 160) -> str:
    cleaned = _REF_SAFE.sub("", value or "")
    return cleaned[:limit]
