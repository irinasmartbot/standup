"""Resend ticket images from admin (Telegram Bot API)."""

from __future__ import annotations

import asyncio
import logging
import os
from html import escape

import psycopg
from psycopg.rows import dict_row

from bot.config import BOOKINGS_SOURCE, DATABASE_URL
from bot.utils.ticket import generate_ticket, guests_word

logger = logging.getLogger(__name__)


def _use_postgres() -> bool:
    return BOOKINGS_SOURCE == "postgres" and bool(DATABASE_URL)


def _bot_token() -> str:
    return (os.getenv("BOT_TOKEN") or "").strip()


def list_event_ticket_holders(event_id: int) -> list[dict]:
    """Confirmed bookings for an event (people who got / should have a ticket)."""
    if not _use_postgres() or not event_id:
        return []
    try:
        with psycopg.connect(DATABASE_URL, row_factory=dict_row) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT
                        b.id AS booking_id,
                        b.guests,
                        b.status,
                        b.ticket_message_id,
                        b.confirmed_at,
                        b.format AS booking_format,
                        b.source AS booking_source,
                        u.id AS user_id,
                        u.telegram_id,
                        u.vk_id,
                        u.name,
                        u.username,
                        u.phone,
                        e.id AS event_id,
                        e.event_date,
                        e.event_time,
                        e.location,
                        e.address
                    FROM bookings b
                    JOIN users u ON u.id = b.user_id
                    JOIN events e ON e.id = b.event_id
                    WHERE b.event_id = %(event_id)s
                      AND b.status = 'confirmed'
                    ORDER BY b.confirmed_at NULLS LAST, b.id
                    """,
                    {"event_id": int(event_id)},
                )
                return [_serialize(row) for row in cur.fetchall()]
    except Exception:
        logger.exception("list_event_ticket_holders failed for %s", event_id)
        return []


def get_booking_for_ticket_resend(booking_id: int) -> dict | None:
    if not _use_postgres() or not booking_id:
        return None
    try:
        with psycopg.connect(DATABASE_URL, row_factory=dict_row) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT
                        b.id AS booking_id,
                        b.guests,
                        b.status,
                        b.ticket_message_id,
                        b.confirmed_at,
                        b.format AS booking_format,
                        b.source AS booking_source,
                        u.id AS user_id,
                        u.telegram_id,
                        u.vk_id,
                        u.name,
                        u.username,
                        u.phone,
                        e.id AS event_id,
                        e.event_date,
                        e.event_time,
                        e.location,
                        e.address
                    FROM bookings b
                    JOIN users u ON u.id = b.user_id
                    JOIN events e ON e.id = b.event_id
                    WHERE b.id = %(booking_id)s
                    """,
                    {"booking_id": int(booking_id)},
                )
                row = cur.fetchone()
                return _serialize(row) if row else None
    except Exception:
        logger.exception("get_booking_for_ticket_resend failed for %s", booking_id)
        return None


def _serialize(row: dict) -> dict:
    d = row.get("event_date")
    t = row.get("event_time")
    return {
        "booking_id": row.get("booking_id"),
        "guests": int(row.get("guests") or 0),
        "status": row.get("status") or "",
        "ticket_message_id": row.get("ticket_message_id"),
        "confirmed_at": row.get("confirmed_at"),
        "booking_format": row.get("booking_format") or "",
        "booking_source": row.get("booking_source") or "",
        "user_id": row.get("user_id"),
        "telegram_id": row.get("telegram_id"),
        "vk_id": row.get("vk_id"),
        "name": row.get("name") or "",
        "username": row.get("username") or "",
        "phone": row.get("phone") or "",
        "event_id": row.get("event_id"),
        "event_date": d.strftime("%d.%m.%Y") if hasattr(d, "strftime") else str(d or ""),
        "event_time": t.strftime("%H:%M") if hasattr(t, "strftime") else str(t or "")[:5],
        "location": row.get("location") or "",
        "address": row.get("address") or "",
        "has_ticket_msg": bool(row.get("ticket_message_id")),
    }


def _ticket_bytes(row: dict) -> bytes:
    address = row.get("address") or ""
    location = row.get("location") or ""
    address_part = address.split(",", 1)[1].strip() if "," in address else address
    short_address = f"{location}, {address_part}".strip(", ")
    buf = generate_ticket(
        row.get("name") or "",
        row.get("event_date") or "",
        row.get("event_time") or "",
        short_address,
        int(row.get("guests") or 1),
    )
    return buf.getvalue()


def _caption(row: dict, *, updated: bool) -> str:
    place = f"{row.get('location') or ''}, {row.get('address') or ''}".strip(", ")
    head = (
        "Обновлённый билет\n\nДанные мероприятия изменились — актуальный билет ниже.\n\n"
        if updated
        else "Билет\n\n"
    )
    return (
        f"{head}"
        f"<b>Данные по билету:</b>\n\n"
        f"<b>Ваше имя:</b> {escape(row.get('name') or '')}\n"
        f"<b>Дата:</b> {escape(row.get('event_date') or '')}\n"
        f"<b>Время:</b> {escape(row.get('event_time') or '')}\n"
        f"<b>Место:</b> {escape(place)}\n"
        f"<b>Количество гостей:</b> {guests_word(int(row.get('guests') or 1))}"
    )


def _is_vk_booking(row: dict) -> bool:
    source = (row.get("booking_source") or "").strip().lower()
    if source in {"vk", "vkontakte"}:
        return True
    return bool(row.get("vk_id")) and not row.get("telegram_id")


async def _resend_ticket_telegram(row: dict, *, updated: bool, extra_note: str = "") -> dict:
    booking_id = int(row["booking_id"])
    token = _bot_token()
    if not token:
        return {"ok": False, "error": "BOT_TOKEN не задан", "booking_id": booking_id}
    telegram_id = row.get("telegram_id")
    if not telegram_id:
        return {"ok": False, "error": "нет telegram_id", "booking_id": booking_id}

    from aiogram import Bot
    from aiogram.types import BufferedInputFile
    from bot.db.crud import save_ticket_message_id

    bot = Bot(token=token)
    try:
        note = (extra_note or "").strip()
        if note:
            await bot.send_message(chat_id=int(telegram_id), text=note)
        photo = BufferedInputFile(
            _ticket_bytes(row),
            filename=f"ticket_{booking_id}.jpg",
        )
        msg = await bot.send_photo(
            chat_id=int(telegram_id),
            photo=photo,
            caption=_caption(row, updated=updated),
            parse_mode="HTML",
        )
        save_ticket_message_id(booking_id, msg.message_id)
        return {"ok": True, "error": "", "booking_id": booking_id}
    finally:
        await bot.session.close()


async def _resend_ticket_vk(row: dict, *, updated: bool, extra_note: str = "") -> dict:
    booking_id = int(row["booking_id"])
    vk_id = row.get("vk_id")
    if not vk_id:
        return {"ok": False, "error": "нет vk_id", "booking_id": booking_id}

    from bot.db.crud import save_ticket_message_id
    from bot.vk.client import VKClient
    from bot.vk.config import load_vk_settings
    from bot.vk.formatting import format_vk_text

    settings = load_vk_settings()
    if not settings.is_configured:
        return {
            "ok": False,
            "error": "VK_GROUP_TOKEN/VK_GROUP_ID не заданы в .env админки",
            "booking_id": booking_id,
        }

    place = f"{row.get('location') or ''}, {row.get('address') or ''}".strip(", ")
    head = (
        "Обновлённый билет\n\nДанные мероприятия изменились — актуальный билет ниже.\n\n"
        if updated
        else "Билет\n\n"
    )
    caption = format_vk_text(
        f"{head}"
        f"<b>Данные по билету:</b>\n\n"
        f"<b>Ваше имя:</b> {row.get('name') or ''}\n"
        f"<b>Дата:</b> {row.get('event_date') or ''}\n"
        f"<b>Время:</b> {row.get('event_time') or ''}\n"
        f"<b>Место:</b> {place}\n"
        f"<b>Количество гостей:</b> {guests_word(int(row.get('guests') or 1))}"
    )
    client = VKClient(settings)
    peer_id = int(vk_id)
    note = (extra_note or "").strip()
    if note:
        await client.send_message(peer_id, note)
    attachment = await client.upload_message_photo(
        peer_id,
        _ticket_bytes(row),
        filename=f"ticket_{booking_id}.jpg",
    )
    msg_id = await client.send_message(peer_id, caption, attachment=attachment)
    save_ticket_message_id(booking_id, msg_id)
    return {"ok": True, "error": "", "booking_id": booking_id}


def _booking_audit_item(row: dict | None, booking_id: int | None = None) -> dict:
    row = row or {}
    return {
        "booking_id": row.get("booking_id") or booking_id,
        "name": (row.get("name") or "").strip(),
        "date": row.get("event_date") or "",
        "time": row.get("event_time") or "",
        "location": (row.get("location") or "").strip(),
        "event_id": row.get("event_id"),
    }


async def resend_ticket_async(
    booking_id: int, *, updated: bool = True, extra_note: str = ""
) -> dict:
    """Send ticket photo to the guest (Telegram or VK). Returns {ok, error, booking_id, ...}."""
    row = get_booking_for_ticket_resend(booking_id)
    meta = _booking_audit_item(row, booking_id)
    if not row:
        return {"ok": False, "error": "бронь не найдена", **meta}
    if row.get("status") != "confirmed":
        return {
            "ok": False,
            "error": f"статус «{row.get('status')}», нужен confirmed",
            **meta,
        }
    try:
        if _is_vk_booking(row):
            result = await _resend_ticket_vk(row, updated=updated, extra_note=extra_note)
        else:
            result = await _resend_ticket_telegram(row, updated=updated, extra_note=extra_note)
        result.update(meta)
        return result
    except Exception as exc:
        logger.exception("resend_ticket failed for booking %s", booking_id)
        return {"ok": False, "error": str(exc), **meta}


def resend_ticket(booking_id: int, *, updated: bool = True, extra_note: str = "") -> dict:
    return asyncio.run(resend_ticket_async(booking_id, updated=updated, extra_note=extra_note))


async def resend_tickets_for_event_async(
    event_id: int, *, updated: bool = True, extra_note: str = ""
) -> dict:
    holders = list_event_ticket_holders(event_id)
    result = {"ok": 0, "fail": 0, "errors": [], "total": len(holders), "items": []}
    for row in holders:
        one = await resend_ticket_async(
            int(row["booking_id"]), updated=updated, extra_note=extra_note
        )
        item = _booking_audit_item(one, one.get("booking_id"))
        item["ok"] = bool(one.get("ok"))
        result["items"].append(item)
        if one.get("ok"):
            result["ok"] += 1
        else:
            result["fail"] += 1
            result["errors"].append(
                f"#{one.get('booking_id')}: {one.get('error') or 'ошибка'}"
            )
        await asyncio.sleep(0.05)
    if holders:
        first = holders[0]
        result["event"] = {
            "id": event_id,
            "date": first.get("event_date") or "",
            "time": first.get("event_time") or "",
            "location": (first.get("location") or "").strip(),
        }
    return result


def resend_tickets_for_event(
    event_id: int, *, updated: bool = True, extra_note: str = ""
) -> dict:
    return asyncio.run(
        resend_tickets_for_event_async(event_id, updated=updated, extra_note=extra_note)
    )
