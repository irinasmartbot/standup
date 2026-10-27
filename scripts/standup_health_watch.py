#!/usr/bin/env python3
"""Check systemd units, disk, Telegram/VK API and Postgres; alert TECH_CHAT_ID.

Intended to run every few minutes via cron on the VPS.
Reads BOT_TOKEN / TECH_CHAT_ID from /home/standup/app/.env (or cwd .env).

  --announce  send the alert catalog once (throttle 24h)
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

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

from bot.utils.health_checks import (  # noqa: E402
    collect_raw_issues,
    empty_state,
    load_watch_state,
    apply_strikes,
)
from bot.utils.systemd_ops import aliases_from_problem_lines, restart_keyboard_json  # noqa: E402
from bot.utils.tech_alerts import (  # noqa: E402
    TECH_ALERT_CATALOG_HTML,
    format_alert,
    notify_tech_sync,
)

DISK_WARN_PERCENT = int(os.getenv("TECH_DISK_WARN_PERCENT", "90"))


def _load_prev_state() -> dict:
    try:
        data = json.loads(PROBLEMS_STATE.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return empty_state()
    return load_watch_state(data)


def _save_state(state: dict) -> None:
    try:
        PROBLEMS_STATE.write_text(
            json.dumps(state, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    except OSError as exc:
        print(f"warn: cannot save state: {exc}", flush=True)


def _announce() -> int:
    sent = notify_tech_sync(
        TECH_ALERT_CATALOG_HTML,
        key="health_announce",
        throttle_sec=86400,
    )
    print(f"announce_sent={sent}", flush=True)
    return 0 if sent else 1


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if "--announce" in args:
        return _announce()

    raw_issues, notes = collect_raw_issues(disk_warn_percent=DISK_WARN_PERCENT)
    prev = _load_prev_state()
    new_state, new_issues, resolved_keys = apply_strikes(prev, raw_issues)

    print("status:", flush=True)
    for line in notes:
        print(f"  {line}", flush=True)
    print(f"prev={prev.get('problems')}", flush=True)
    print(f"now={new_state.get('problems')}", flush=True)
    print(f"new={[issue.key for issue in new_issues]}", flush=True)
    print(f"resolved={resolved_keys}", flush=True)

    if new_issues:
        meaning_lines = []
        for issue in new_issues:
            meaning_lines.append(f"{issue.detail}\n{issue.meaning}")
        meaning = "\n\n".join(meaning_lines)
        meaning += (
            "\n\nМожно нажать Restart ниже (только если упал сервис) или в чате:\n"
            "/tech_status\n/tech_restart vk|bot|admin\n/tech_help"
        )
        body = "\n".join(issue.detail for issue in new_issues)
        kb = restart_keyboard_json(aliases_from_problem_lines(issue.detail for issue in new_issues))
        sent = notify_tech_sync(
            format_alert("Сервер: проблема", body, source="health-watch", meaning=meaning),
            key="health_new:" + "|".join(issue.key for issue in new_issues),
            throttle_sec=120,
            reply_markup=kb,
        )
        print(f"alert_problem_sent={sent}", flush=True)

    if resolved_keys:
        labels = []
        now_problems = new_state.get("problems") or {}
        prev_problems = prev.get("problems") or {}
        for key in resolved_keys:
            labels.append(prev_problems.get(key, key))
        body = "Снова в норме:\n" + "\n".join(f"• {item}" for item in labels)
        extra = ""
        if now_problems:
            extra = "Ещё не ок:\n" + "\n".join(f"• {v}" for v in now_problems.values())
        sent = notify_tech_sync(
            format_alert(
                "Сервер: проблема устранена",
                extra,
                source="health-watch",
                meaning=(
                    "Проверка снова зелёная. Это не значит, что гости уже всё получили — "
                    "только что сервис/API/база снова отвечают.\n\n" + body
                ),
            ),
            key="health_ok:" + "|".join(resolved_keys),
            throttle_sec=60,
        )
        print(f"alert_resolved_sent={sent}", flush=True)

    if not new_issues and not resolved_keys:
        print("no state change", flush=True)

    _save_state(new_state)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
