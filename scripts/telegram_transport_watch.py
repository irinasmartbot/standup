#!/usr/bin/env python3
"""Once-a-minute personal Telegram route alerts with a persistent outbox."""
from __future__ import annotations

import argparse
import fcntl
import json
import os
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def save_state(path: Path, value: dict) -> None:
    fd, tmp = tempfile.mkstemp(prefix=".telegram-monitor-", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as out:
            json.dump(value, out, ensure_ascii=False)
            out.flush()
            os.fsync(out.fileno())
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--probe-only", action="store_true")
    group.add_argument("--test-alert", action="store_true")
    args = parser.parse_args(argv)
    from dotenv import load_dotenv
    load_dotenv(ROOT / ".env")
    from bot.utils.telegram_monitor import deliver_pending, probe_routes, send_alert, update_state

    token = os.getenv("BOT_TOKEN", "")
    if not token:
        raise SystemExit("BOT_TOKEN is required")
    if args.probe_only:
        print(json.dumps(probe_routes(token)))
        return 0
    chat_id = int(os.getenv("TELEGRAM_MONITOR_CHAT_ID", "0"))
    if chat_id <= 0:
        raise SystemExit("A personal TELEGRAM_MONITOR_CHAT_ID is required")
    if args.test_alert:
        ok = send_alert(token, chat_id, "Тест алертов стендапа. Уведомления о связи с Telegram подключены. Это проверка доставки, не авария.")
        print(json.dumps({"test_alert_delivered": ok}))
        return 0 if ok else 1

    path = Path(os.getenv("TELEGRAM_MONITOR_STATE", "/home/standup/state/telegram-transport.json"))
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    with path.with_suffix(".lock").open("a") as lock:
        os.chmod(lock.name, 0o600)
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print('{"skipped":"already_running"}')
            return 0
        if path.exists():
            # A corrupt outbox must not silently erase unsent incidents.
            state = json.loads(path.read_text())
        else:
            state = {}
        checks = probe_routes(token)
        state = update_state(state, checks, int(time.time()))
        save_state(path, state)
        delivered = deliver_pending(state, lambda text: send_alert(token, chat_id, text), lambda value: save_state(path, value))
        print(json.dumps({"routes": checks, "alerts_delivered": delivered, "alerts_pending": len(state["pending"])}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
