#!/usr/bin/env python3
"""Check systemd units + disk; alert TECH_CHAT_ID on problems and recoveries.

Intended to run every few minutes via systemd timer on the VPS.
Reads BOT_TOKEN / TECH_CHAT_ID from /home/standup/app/.env (or cwd .env).
"""

from __future__ import annotations

import json
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
PROBLEMS_STATE = Path(
    os.getenv("TECH_HEALTH_STATE_PATH", "/tmp/standup_health_problems.json")
)


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


def _load_prev_problems() -> list[str]:
    try:
        data = json.loads(PROBLEMS_STATE.read_text(encoding="utf-8"))
        if isinstance(data, list):
            return [str(x) for x in data]
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        pass
    return []


def _save_problems(problems: list[str]) -> None:
    try:
        PROBLEMS_STATE.write_text(
            json.dumps(problems, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    except OSError as exc:
        print(f"warn: cannot save state: {exc}", flush=True)


def _collect_problems() -> tuple[list[str], list[str]]:
    problems: list[str] = []
    notes: list[str] = []
    for unit in UNITS:
        load_state = _unit_load_state(unit)
        if load_state == "not-found":
            notes.append(f"{unit}: not-found (skip)")
            continue
        status = _systemctl_is_active(unit)
        notes.append(f"{unit}: load={load_state} active={status}")
        if status != "active":
            problems.append(f"{unit}: {status}")

    disk_pct = _disk_usage_percent("/")
    if disk_pct is not None:
        notes.append(f"disk / used {disk_pct}%")
        if disk_pct >= DISK_WARN_PERCENT:
            problems.append(f"disk / used {disk_pct}% (limit {DISK_WARN_PERCENT}%)")
    return problems, notes


def main() -> int:
    problems, notes = _collect_problems()
    prev = _load_prev_problems()
    prev_set = set(prev)
    now_set = set(problems)

    new_problems = sorted(now_set - prev_set)
    resolved = sorted(prev_set - now_set)

    print("status:", flush=True)
    for line in notes:
        print(f"  {line}", flush=True)
    print(f"prev={prev}", flush=True)
    print(f"now={problems}", flush=True)
    print(f"new={new_problems}", flush=True)
    print(f"resolved={resolved}", flush=True)

    if new_problems:
        body = "\n".join(new_problems)
        sent = notify_tech_sync(
            format_alert("Сервер: проблема", body, source="health-watch"),
            key="health_new:" + "|".join(new_problems),
            throttle_sec=120,
        )
        print(f"alert_problem_sent={sent}", flush=True)

    if resolved:
        body = "\n".join(resolved)
        sent = notify_tech_sync(
            format_alert("Сервер: проблема устранена", body, source="health-watch"),
            key="health_ok:" + "|".join(resolved),
            throttle_sec=60,
        )
        print(f"alert_resolved_sent={sent}", flush=True)

    if not new_problems and not resolved:
        print("no state change", flush=True)

    _save_problems(problems)
    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(main())
