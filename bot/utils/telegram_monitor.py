"""Durable transport incidents. Does not import the bot or query application data."""
from __future__ import annotations

import http.client
import json
import time
from datetime import datetime
from zoneinfo import ZoneInfo

from bot.utils.telegram_relay import (
    _https_post_form,
    telegram_api_post_form,
    telegram_relay_settings,
)


def probe_routes(token: str, timeout: float = 5) -> dict[str, bool]:
    result = {}
    for route in ("direct", "relay"):
        settings = telegram_relay_settings() if route == "relay" else None
        if route == "relay" and not settings.enabled:
            result[route] = False
            continue
        try:
            status, body = _https_post_form(token, "getMe", {}, timeout=timeout, relay=settings)
            result[route] = status == 200 and json.loads(body).get("ok") is True
        except (OSError, ValueError, http.client.HTTPException):
            result[route] = False
    return result


def incident_text(checks: dict[str, bool], previous: dict, now: int) -> str:
    at = datetime.fromtimestamp(now, ZoneInfo("Europe/Moscow")).strftime("%d.%m %H:%M МСК")
    if checks["direct"] and checks["relay"]:
        message = "Оба соединения доступны. Основной путь — напрямую; туннель готов как резерв."
    elif checks["direct"]:
        message = "Прямое соединение работает. Резервный туннель недоступен: запасного пути сейчас нет."
    elif checks["relay"]:
        message = "Прямое соединение недоступно. Работает резервный туннель; бот автоматически переходит на него."
    else:
        message = "Оба соединения с Telegram недоступны. Бот может не отвечать. Это уведомление сохранено до восстановления связи."
    if previous.get("direct") is False and checks["direct"]:
        message = "Прямое соединение восстановлено. Бот возвращается на него в течение минуты.\n" + message
    return f"Стендап · связь с Telegram\n{at}\n{message}"


def update_state(state: dict, checks: dict[str, bool], now: int, confirmations: int = 2) -> dict:
    # Round-trip copy also keeps callers' previous observations immutable.
    state = json.loads(json.dumps(state))
    known = state.setdefault("known", {})
    previous = dict(known)
    candidates = state.setdefault("candidates", {})
    pending = state.setdefault("pending", [])
    for key, value in checks.items():
        candidate = candidates.get(key, {})
        count = candidate.get("count", 0) + 1 if candidate.get("value") is value else 1
        candidates[key] = {"value": value, "count": min(count, confirmations)}
        if count >= confirmations and known.get(key) is not value:
            known[key] = value
    complete = all(key in known for key in ("direct", "relay"))
    initialized = all(key in previous for key in ("direct", "relay"))
    changed = complete and known != previous and (initialized or not all(known.values()))
    if changed:
        state["sequence"] = state.get("sequence", 0) + 1
        pending.append({"id": state["sequence"], "created_at": now, "text": incident_text(known, previous, now)})
    state["last_checked_at"] = now
    state["observed"] = checks
    return state


def send_alert(token: str, chat_id: int, text: str) -> bool:
    try:
        status, body = telegram_api_post_form(
            token, "sendMessage", {"chat_id": str(chat_id), "text": text}, timeout=8,
        )
        return status == 200 and json.loads(body).get("ok") is True
    except (OSError, ValueError, http.client.HTTPException):
        return False


def deliver_pending(state: dict, send, save) -> int:
    delivered = 0
    while state.get("pending"):
        event = state["pending"][0]
        if not send(event["text"]):
            break
        state["pending"].pop(0)
        state["last_delivered_id"] = event["id"]
        state["last_delivered_at"] = int(time.time())
        save(state)
        delivered += 1
    return delivered
