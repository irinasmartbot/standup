#!/usr/bin/env python3
"""Check systemd units + disk; alert TECH_CHAT_ID on problems.

Intended to run every few minutes via systemd timer on the VPS.
Reads BOT_TOKEN / TECH_CHAT_ID from /home/standup/app/.env (or cwd .env).
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

# Load app .env before importing tech_alerts helpers that read os.environ.
APP_ENV = Path("/home/standup/app/.env")
LOCAL_ENV = ROOT / ".env"


def _load_env(path: Path) -> None:
    if not path.is_file():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


_load_env(APP_ENV)
_load_env(LOCAL_ENV)

from bot.utils.tech_alerts import format_alert, notify_tech_sync  # noqa: E402

UNITS = (
    "standup-bot.service",
    "standup-vk-bot.service",
    "standup-admin.service",
)
DISK_WARN_PERCENT = int(os.getenv("TECH_DISK_WARN_PERCENT", "90"))


def _systemctl_is_active(unit: str) -> str:
    try:
        proc = subprocess.run(
            ["systemctl", "is-active", unit],
            check=False,
            capture_output=True,
            text=True,
            timeout=15,
        )
        return (proc.stdout or proc.stderr or "").strip() or "unknown"
    except (OSError, subprocess.TimeoutExpired) as exc:
        return f"check-failed:{exc}"


def _disk_usage_percent(path: str = "/") -> int | None:
    try:
        usage = shutil.disk_usage(path)
        if usage.total <= 0:
            return None
        return int(round(100.0 * (usage.total - usage.free) / usage.total))
    except OSError:
        return None


def main() -> int:
    problems: list[str] = []
    for unit in UNITS:
        # Skip missing units (e.g. admin not installed on a host).
        listed = subprocess.run(
            ["systemctl", "list-unit-files", unit, "--no-legend"],
            check=False,
            capture_output=True,
            text=True,
            timeout=15,
        )
        if unit not in (listed.stdout or ""):
            continue
        status = _systemctl_is_active(unit)
        if status != "active":
            problems.append(f"{unit}: {status}")

    disk_pct = _disk_usage_percent("/")
    if disk_pct is not None and disk_pct >= DISK_WARN_PERCENT:
        problems.append(f"disk / used {disk_pct}% (limit {DISK_WARN_PERCENT}%)")

    if not problems:
        print("ok")
        return 0

    body = "\n".join(problems)
    print(body)
    notify_tech_sync(
        format_alert("Сервер: проблема со здоровьем", body, source="health-watch"),
        key="health:" + "|".join(problems),
        throttle_sec=600,
    )
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
