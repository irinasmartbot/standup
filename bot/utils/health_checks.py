"""Collect health problems for the tech-chat watchdog.

Network/DB checks need two consecutive failures before they become an alert.
Systemd and disk alert on the first failure.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from dataclasses import dataclass
from typing import Iterable
from urllib.parse import urlencode
from urllib.request import urlopen

UNITS = (
    "standup-bot.service",
    "standup-vk-bot.service",
    "standup-admin.service",
)

UNIT_MEANING = {
    "standup-bot.service": (
        "Процесс Telegram-бота на сервере не запущен. "
        "Гости в Telegram не получают ответы, брони и билеты не уходят."
    ),
    "standup-vk-bot.service": (
        "Процесс VK-бота не запущен. Гости ВКонтакте не получают ответы."
    ),
    "standup-admin.service": (
        "Админка не запущена. Ссылка go.moscowstandupshow.ru может не открываться. "
        "Боты при этом могут ещё отвечать."
    ),
}

DISK_MEANING = (
    "На диске сервера почти не осталось места. "
    "Могут перестать писаться логи, база и картинки билетов."
)
TELEGRAM_API_MEANING = (
    "Сервер не достучался до Telegram API (getMe). "
    "Если это сообщение пришло — канал частично жив, но бот может тормозить "
    "или не получать новые сообщения."
)
VK_API_MEANING = (
    "Сервер не достучался до VK API. Бот ВКонтакте может не отвечать гостям."
)
POSTGRES_MEANING = (
    "База данных не отвечает. Новые брони, билеты и админка будут ломаться."
)

API_STRIKES_NEEDED = 2


@dataclass(frozen=True)
class HealthIssue:
    key: str
    detail: str
    meaning: str
    strikes_needed: int = 1


def _run(cmd: list[str]) -> str:
    try:
        proc = subprocess.run(
            cmd,
            check=False,
            capture_output=True,
            text=True,
            timeout=15,
        )
        return (proc.stdout or "").strip()
    except (OSError, subprocess.TimeoutExpired) as exc:
        return f"check-failed:{exc}"


def _unit_load_state(unit: str) -> str:
    return _run(["systemctl", "show", unit, "-p", "LoadState", "--value"]) or "unknown"


def _systemctl_is_active(unit: str) -> str:
    return _run(["systemctl", "is-active", unit]) or "unknown"


def _disk_usage_percent(path: str = "/") -> int | None:
    try:
        usage = shutil.disk_usage(path)
        if usage.total <= 0:
            return None
        return int(round(100.0 * (usage.total - usage.free) / usage.total))
    except OSError:
        return None


def collect_unit_issues() -> tuple[list[HealthIssue], list[str]]:
    issues: list[HealthIssue] = []
    notes: list[str] = []
    for unit in UNITS:
        load_state = _unit_load_state(unit)
        if load_state == "not-found":
            notes.append(f"{unit}: not-found (skip)")
            continue
        status = _systemctl_is_active(unit)
        notes.append(f"{unit}: load={load_state} active={status}")
        if status != "active":
            issues.append(
                HealthIssue(
                    key=f"unit:{unit}",
                    detail=f"{unit}: {status}",
                    meaning=UNIT_MEANING.get(unit, "Сервис на сервере не запущен."),
                    strikes_needed=1,
                )
            )
    return issues, notes


def collect_disk_issue(warn_percent: int) -> tuple[HealthIssue | None, str]:
    disk_pct = _disk_usage_percent("/")
    if disk_pct is None:
        return None, "disk / unknown"
    note = f"disk / used {disk_pct}%"
    if disk_pct >= warn_percent:
        return (
            HealthIssue(
                key="disk",
                detail=f"disk / used {disk_pct}% (limit {warn_percent}%)",
                meaning=DISK_MEANING,
                strikes_needed=1,
            ),
            note,
        )
    return None, note


def collect_telegram_api_issue() -> tuple[HealthIssue | None, str]:
    token = (os.getenv("BOT_TOKEN") or "").strip()
    if not token:
        return None, "telegram_api: skip (no BOT_TOKEN)"
    try:
        from bot.utils.telegram_relay import telegram_api_post_form

        status, body = telegram_api_post_form(token, "getMe", {}, timeout=12)
        payload = json.loads(body.decode("utf-8", errors="replace") or "{}")
        if status >= 400 or not payload.get("ok"):
            desc = payload.get("description") or f"HTTP {status}"
            return (
                HealthIssue(
                    key="telegram_api",
                    detail=f"telegram getMe: {desc}",
                    meaning=TELEGRAM_API_MEANING,
                    strikes_needed=API_STRIKES_NEEDED,
                ),
                f"telegram_api: fail {desc}",
            )
        return None, "telegram_api: ok"
    except (TimeoutError, OSError, ValueError, json.JSONDecodeError) as exc:
        return (
            HealthIssue(
                key="telegram_api",
                detail=f"telegram getMe: {type(exc).__name__}: {exc}",
                meaning=TELEGRAM_API_MEANING,
                strikes_needed=API_STRIKES_NEEDED,
            ),
            f"telegram_api: error {type(exc).__name__}",
        )


def collect_vk_api_issue() -> tuple[HealthIssue | None, str]:
    enabled = (os.getenv("VK_ENABLED") or "").strip().lower() in {"1", "true", "yes", "on"}
    token = (os.getenv("VK_GROUP_TOKEN") or "").strip()
    group_id = (os.getenv("VK_GROUP_ID") or "").strip()
    version = (os.getenv("VK_API_VERSION") or "5.199").strip() or "5.199"
    if not enabled or not token or not group_id:
        return None, "vk_api: skip (not configured)"
    query = urlencode(
        {
            "group_id": group_id.lstrip("-"),
            "access_token": token,
            "v": version,
        }
    )
    url = f"https://api.vk.com/method/groups.getById?{query}"
    try:
        with urlopen(url, timeout=12) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
        payload = json.loads(raw or "{}")
        error = payload.get("error") if isinstance(payload, dict) else None
        if isinstance(error, dict):
            code = error.get("error_code")
            msg = error.get("error_msg") or "vk error"
            return (
                HealthIssue(
                    key="vk_api",
                    detail=f"vk groups.getById: {code} {msg}",
                    meaning=VK_API_MEANING,
                    strikes_needed=API_STRIKES_NEEDED,
                ),
                f"vk_api: fail {code}",
            )
        if payload.get("response") in (None, [], {}):
            return (
                HealthIssue(
                    key="vk_api",
                    detail="vk groups.getById: empty response",
                    meaning=VK_API_MEANING,
                    strikes_needed=API_STRIKES_NEEDED,
                ),
                "vk_api: empty",
            )
        return None, "vk_api: ok"
    except (TimeoutError, OSError, ValueError, json.JSONDecodeError) as exc:
        return (
            HealthIssue(
                key="vk_api",
                detail=f"vk groups.getById: {type(exc).__name__}: {exc}",
                meaning=VK_API_MEANING,
                strikes_needed=API_STRIKES_NEEDED,
            ),
            f"vk_api: error {type(exc).__name__}",
        )


def collect_postgres_issue() -> tuple[HealthIssue | None, str]:
    dsn = (os.getenv("DATABASE_URL") or "").strip()
    if not dsn:
        return None, "postgres: skip (no DATABASE_URL)"
    try:
        import psycopg

        with psycopg.connect(dsn, connect_timeout=8) as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT 1")
                cur.fetchone()
        return None, "postgres: ok"
    except Exception as exc:
        return (
            HealthIssue(
                key="postgres",
                detail=f"postgres: {type(exc).__name__}",
                meaning=POSTGRES_MEANING,
                strikes_needed=API_STRIKES_NEEDED,
            ),
            f"postgres: error {type(exc).__name__}",
        )


def collect_raw_issues(*, disk_warn_percent: int) -> tuple[list[HealthIssue], list[str]]:
    issues: list[HealthIssue] = []
    notes: list[str] = []
    unit_issues, unit_notes = collect_unit_issues()
    issues.extend(unit_issues)
    notes.extend(unit_notes)
    disk_issue, disk_note = collect_disk_issue(disk_warn_percent)
    notes.append(disk_note)
    if disk_issue:
        issues.append(disk_issue)
    for collector in (collect_telegram_api_issue, collect_vk_api_issue, collect_postgres_issue):
        issue, note = collector()
        notes.append(note)
        if issue:
            issues.append(issue)
    return issues, notes


def empty_state() -> dict:
    return {"problems": {}, "strikes": {}}


def load_watch_state(data: object) -> dict:
    """Accept new dict state or the old list-of-strings format."""
    if isinstance(data, dict) and ("problems" in data or "strikes" in data):
        problems = data.get("problems") or {}
        strikes = data.get("strikes") or {}
        if isinstance(problems, dict) and isinstance(strikes, dict):
            return {
                "problems": {str(k): str(v) for k, v in problems.items()},
                "strikes": {str(k): int(v) for k, v in strikes.items()},
            }
    if isinstance(data, list):
        problems = {}
        for item in data:
            text = str(item)
            key = text.split(":", 1)[0].strip() or text
            if key in {unit for unit in UNITS}:
                key = f"unit:{key}"
            problems[key] = text
        return {"problems": problems, "strikes": {k: 1 for k in problems}}
    return empty_state()


def apply_strikes(prev_state: dict, raw_issues: Iterable[HealthIssue]) -> tuple[dict, list[HealthIssue], list[str]]:
    """Turn raw checks into firing alerts after the needed consecutive failures."""
    prev = load_watch_state(prev_state)
    raw_map = {issue.key: issue for issue in raw_issues}
    strikes = dict(prev.get("strikes") or {})
    prev_problems = dict(prev.get("problems") or {})
    now_problems: dict[str, str] = {}
    firing: list[HealthIssue] = []

    for key, issue in raw_map.items():
        count = int(strikes.get(key, 0)) + 1
        strikes[key] = count
        if count >= issue.strikes_needed:
            now_problems[key] = issue.detail
            firing.append(issue)

    for key in list(strikes):
        if key not in raw_map:
            strikes.pop(key, None)

    resolved = sorted(set(prev_problems) - set(now_problems))
    new_issues = [issue for issue in firing if issue.key not in prev_problems]
    new_state = {"problems": now_problems, "strikes": strikes}
    return new_state, new_issues, resolved
