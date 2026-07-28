"""Notify guests about event cancel / hide from admin."""

from __future__ import annotations

import asyncio
import logging
import os

import psycopg
from psycopg.rows import dict_row

from bot.config import BOOKINGS_SOURCE, DATABASE_URL

logger = logging.getLogger(__name__)


def _use_postgres() -> bool:
    return BOOKINGS_SOURCE == "postgres" and bool(DATABASE_URL)


def _bot_token() -> str:
    return (os.getenv("BOT_TOKEN") or "").strip()


def list_event_notify_recipients(event_id: int, audience: str) -> list[dict]:
    """audience: booked | confirmed | both"""
    if not _use_postgres() or not event_id:
        return []
    statuses = {
        "booked": ("booked",),
        "confirmed": ("confirmed",),
        "both": ("booked", "confirmed"),
    }.get(audience or "", ())
    if not statuses:
        return []
    try:
        with psycopg.connect(DATABASE_URL, row_factory=dict_row) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT
                        b.id AS booking_id,
                        b.status,
                        b.source AS booking_source,
                        u.telegram_id,
                        u.vk_id,
                        u.name,
                        e.event_date,
                        e.event_time,
                        e.location
                    FROM bookings b
                    JOIN users u ON u.id = b.user_id
                    JOIN events e ON e.id = b.event_id
                    WHERE b.event_id = %(event_id)s
                      AND b.status = ANY(%(statuses)s)
                    ORDER BY b.id
                    """,
                    {"event_id": int(event_id), "statuses": list(statuses)},
                )
                return [dict(row) for row in cur.fetchall()]
    except Exception:
        logger.exception("list_event_notify_recipients failed for %s", event_id)
        return []


async def _send_tg(telegram_id: int, text: str) -> None:
    from aiogram import Bot

    token = _bot_token()
    if not token:
        raise RuntimeError("BOT_TOKEN не задан")
    bot = Bot(token=token)
    try:
        await bot.send_message(chat_id=int(telegram_id), text=text)
    finally:
        await bot.session.close()


async def _send_vk(vk_id: int, text: str) -> None:
    from bot.vk.client import VKClient
    from bot.vk.config import load_vk_settings

    settings = load_vk_settings()
    if not settings.is_configured:
        raise RuntimeError("VK не настроен")
    client = VKClient(settings)
    await client.send_message(int(vk_id), text)


async def notify_event_guests_async(
    event_ids: list[int],
    message: str,
    audience: str,
) -> dict:
    text = (message or "").strip()
    if not text or not audience or not event_ids:
        return {"ok": 0, "fail": 0, "skipped": True}
    result = {"ok": 0, "fail": 0, "errors": [], "skipped": False}
    seen: set[tuple[str, int]] = set()
    for eid in event_ids:
        for row in list_event_notify_recipients(int(eid), audience):
            source = (row.get("booking_source") or "").strip().lower()
            tid = row.get("telegram_id")
            vid = row.get("vk_id")
            try:
                if source in {"vk", "vkontakte"} or (vid and not tid):
                    if not vid:
                        raise RuntimeError("нет vk_id")
                    key = ("vk", int(vid))
                    if key in seen:
                        continue
                    seen.add(key)
                    await _send_vk(int(vid), text)
                else:
                    if not tid:
                        raise RuntimeError("нет telegram_id")
                    key = ("tg", int(tid))
                    if key in seen:
                        continue
                    seen.add(key)
                    await _send_tg(int(tid), text)
                result["ok"] += 1
            except Exception as exc:
                result["fail"] += 1
                result["errors"].append(f"#{row.get('booking_id')}: {exc}")
            await asyncio.sleep(0.05)
    return result
