"""Alerts to a dedicated Telegram tech chat.

Uses BOT_TOKEN + TECH_CHAT_ID. Safe to call from TG/VK bots and from
systemd/cron scripts (sync helper does not need a running aiogram loop).
"""

from __future__ import annotations

import json
import logging
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from html import escape
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_DEFAULT_THROTTLE_SEC = 300
_STATE_PATH = Path(
    os.getenv("TECH_ALERTS_STATE_PATH", "/tmp/standup_tech_alerts_state.json")
)
_SENDING = False


def tech_chat_id() -> int | None:
    raw = (os.getenv("TECH_CHAT_ID") or "").strip()
    if not raw:
        return None
    try:
        return int(raw)
    except ValueError:
        logger.error("TECH_CHAT_ID is not an integer: %r", raw)
        return None


def bot_token() -> str:
    return (os.getenv("BOT_TOKEN") or "").strip()


def _load_state() -> dict[str, float]:
    try:
        data = json.loads(_STATE_PATH.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            return {str(k): float(v) for k, v in data.items()}
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        pass
    return {}


def _save_state(state: dict[str, float]) -> None:
    try:
        _STATE_PATH.write_text(json.dumps(state), encoding="utf-8")
    except OSError:
        logger.exception("Failed to save tech alerts state")


def _throttled(key: str | None, throttle_sec: int) -> bool:
    if not key or throttle_sec <= 0:
        return False
    state = _load_state()
    now = time.time()
    last = state.get(key, 0.0)
    if now - last < throttle_sec:
        return True
    state[key] = now
    # Drop stale keys so the file does not grow forever.
    cutoff = now - max(throttle_sec * 4, 3600)
    state = {k: v for k, v in state.items() if v >= cutoff}
    _save_state(state)
    return False


def format_alert(title: str, body: str = "", *, source: str = "") -> str:
    parts = [f"<b>{escape(title)}</b>"]
    if source:
        parts.append(f"<i>{escape(source)}</i>")
    if body:
        clipped = body.strip()
        if len(clipped) > 3500:
            clipped = clipped[:3500] + "…"
        parts.append(f"<pre>{escape(clipped)}</pre>")
    return "\n".join(parts)


def notify_tech_sync(
    text: str,
    *,
    key: str | None = None,
    throttle_sec: int = _DEFAULT_THROTTLE_SEC,
) -> bool:
    """Send HTML text to TECH_CHAT_ID via Bot API. Returns True if sent."""
    global _SENDING
    chat_id = tech_chat_id()
    token = bot_token()
    if not chat_id or not token:
        return False
    if _throttled(key, throttle_sec):
        return False
    if _SENDING:
        return False
    _SENDING = True
    try:
        payload = urllib.parse.urlencode(
            {
                "chat_id": str(chat_id),
                "text": text,
                "parse_mode": "HTML",
                "disable_web_page_preview": "true",
            }
        ).encode("utf-8")
        req = urllib.request.Request(
            f"https://api.telegram.org/bot{token}/sendMessage",
            data=payload,
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=15) as resp:
            resp.read()
        return True
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        logger.warning("tech alert send failed: %s", exc)
        return False
    finally:
        _SENDING = False


async def notify_tech(
    text: str,
    *,
    key: str | None = None,
    throttle_sec: int = _DEFAULT_THROTTLE_SEC,
    bot: Any | None = None,
) -> bool:
    """Async send; prefers aiogram bot if given, else sync Bot API."""
    chat_id = tech_chat_id()
    if not chat_id:
        return False
    if _throttled(key, throttle_sec):
        return False
    if bot is not None:
        global _SENDING
        if _SENDING:
            return False
        _SENDING = True
        try:
            kwargs: dict = {"chat_id": chat_id, "text": text, "parse_mode": "HTML"}
            try:
                from aiogram.types import LinkPreviewOptions

                kwargs["link_preview_options"] = LinkPreviewOptions(is_disabled=True)
            except Exception:
                pass
            await bot.send_message(**kwargs)
            return True
        except Exception as exc:
            logger.warning("tech alert async send failed: %s", exc)
            return False
        finally:
            _SENDING = False
    return notify_tech_sync(text, key=None, throttle_sec=0)


async def notify_exception(
    title: str,
    exc: BaseException,
    *,
    source: str = "",
    key: str | None = None,
    bot: Any | None = None,
    throttle_sec: int = _DEFAULT_THROTTLE_SEC,
) -> bool:
    body = f"{type(exc).__name__}: {exc}"
    text = format_alert(title, body, source=source)
    return await notify_tech(text, key=key, throttle_sec=throttle_sec, bot=bot)
