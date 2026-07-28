"""Notify guests and cancel bookings when an event is hidden/deleted from admin."""

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
                        b.format AS booking_format,
                        b.source AS booking_source,
                        b.ticket_message_id,
                        b.confirm_message_id,
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


async def _delete_tg_ticket(telegram_id: int, ticket_message_id, confirm_message_id) -> None:
    from aiogram import Bot

    token = _bot_token()
    if not token or not telegram_id:
        return
    bot = Bot(token=token)
    try:
        if confirm_message_id:
            try:
                await bot.edit_message_reply_markup(
                    chat_id=int(telegram_id),
                    message_id=int(confirm_message_id),
                    reply_markup=None,
                )
            except Exception:
                pass
        if ticket_message_id:
            try:
                await bot.delete_message(chat_id=int(telegram_id), message_id=int(ticket_message_id))
            except Exception:
                try:
                    await bot.send_message(
                        chat_id=int(telegram_id),
                        text="❌ Ваш электронный билет аннулирован в связи с отменой мероприятия.",
                    )
                except Exception:
                    pass
    finally:
        await bot.session.close()


async def _delete_vk_ticket(vk_id: int, ticket_message_id) -> None:
    if not vk_id or not ticket_message_id:
        return
    from bot.vk.client import VKClient
    from bot.vk.config import load_vk_settings

    settings = load_vk_settings()
    if not settings.is_configured:
        return
    client = VKClient(settings)
    try:
        await client.delete_messages(int(vk_id), [int(ticket_message_id)])
    except Exception:
        pass


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


async def cancel_event_bookings_async(event_ids: list[int]) -> dict:
    """Cancel booked+confirmed for hidden events: stop reminders, void tickets."""
    if not event_ids:
        return {"cancelled": 0, "fail": 0, "errors": [], "skipped": True}
    from bot.db.crud import get_active_raffle_booking, set_rozygrysh_used, update_booking_status

    result = {"cancelled": 0, "fail": 0, "errors": [], "skipped": False}
    for eid in event_ids:
        rows = list_event_notify_recipients(int(eid), "both")
        for row in rows:
            booking_id = row.get("booking_id")
            if not booking_id:
                continue
            source = (row.get("booking_source") or "").strip().lower()
            tid = row.get("telegram_id")
            vid = row.get("vk_id")
            try:
                if source in {"vk", "vkontakte"} or (vid and not tid):
                    if vid:
                        await _delete_vk_ticket(int(vid), row.get("ticket_message_id"))
                elif tid:
                    await _delete_tg_ticket(
                        int(tid),
                        row.get("ticket_message_id"),
                        row.get("confirm_message_id"),
                    )
                update_booking_status(int(booking_id), "cancelled")
                if (row.get("booking_format") or "") == "rozygrysh":
                    try:
                        if source in {"vk", "vkontakte"} or (vid and not tid):
                            if vid and not get_active_raffle_booking(vk_id=int(vid)):
                                set_rozygrysh_used(vk_id=int(vid), used=False)
                        elif tid and not get_active_raffle_booking(int(tid)):
                            set_rozygrysh_used(int(tid), False)
                    except Exception:
                        logger.exception("raffle flag reset failed for booking %s", booking_id)
                result["cancelled"] += 1
            except Exception as exc:
                result["fail"] += 1
                result["errors"].append(f"#{booking_id}: {exc}")
                logger.exception("cancel booking %s for event %s failed", booking_id, eid)
            await asyncio.sleep(0.05)
    return result


def _h(value) -> str:
    return (
        str(value or "")
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def _person_channel(row: dict) -> str:
    source = (row.get("booking_source") or "").strip().lower()
    tid = row.get("telegram_id")
    vid = row.get("vk_id")
    if source in {"vk", "vkontakte"} or (vid and not tid):
        return f"VK {vid}" if vid else "VK"
    if tid:
        return f"TG {tid}"
    return "—"


def _event_label(row: dict) -> str:
    date = row.get("event_date")
    time = row.get("event_time")
    loc = row.get("location") or ""
    date_s = date.isoformat() if hasattr(date, "isoformat") else str(date or "")
    time_s = time.strftime("%H:%M") if hasattr(time, "strftime") else str(time or "")[:5]
    return f"{date_s} {time_s} · {loc}".strip(" ·")


def build_hide_impact(event_ids: list[int], audience: str = "") -> dict:
    """Preview who is affected when hiding events (cancel + optional notify)."""
    ids = []
    for raw in event_ids or []:
        try:
            ids.append(int(raw))
        except (TypeError, ValueError):
            pass
    ids = list(dict.fromkeys(ids))
    if not ids:
        return {
            "event_ids": [],
            "cancel_count": 0,
            "ticket_count": 0,
            "notify_count": 0,
            "people": [],
            "html": "",
        }

    people = []
    ticket_count = 0
    notify_set: set[int] = set()
    notify_statuses = {
        "booked": {"booked"},
        "confirmed": {"confirmed"},
        "both": {"booked", "confirmed"},
    }.get((audience or "").strip(), set())

    for eid in ids:
        for row in list_event_notify_recipients(eid, "both"):
            status = row.get("status") or ""
            has_ticket = status == "confirmed" or bool(row.get("ticket_message_id"))
            if has_ticket:
                ticket_count += 1
            will_notify = status in notify_statuses
            if will_notify:
                notify_set.add(int(row["booking_id"]))
            people.append(
                {
                    "booking_id": row.get("booking_id"),
                    "event_id": eid,
                    "event_label": _event_label(row),
                    "name": row.get("name") or "Без имени",
                    "channel": _person_channel(row),
                    "status": status,
                    "status_label": "билет" if status == "confirmed" else "бронь",
                    "has_ticket": has_ticket,
                    "will_notify": will_notify,
                }
            )

    html = _render_impact_html(people, len(ids), ticket_count, len(notify_set), bool(notify_statuses))
    return {
        "event_ids": ids,
        "cancel_count": len(people),
        "ticket_count": ticket_count,
        "notify_count": len(notify_set),
        "people": people,
        "html": html,
    }


def _render_impact_html(
    people: list[dict],
    event_count: int,
    ticket_count: int,
    notify_count: int,
    notify_enabled: bool,
) -> str:
    if not people:
        return (
            '<div class="events-impact-box events-impact-empty">'
            "<b>Превью скрытия</b>"
            "<p>На отмеченных шоу нет активных броней и билетов — "
            "отменять и писать будет некому.</p>"
            "</div>"
        )

    rows = []
    for p in people:
        effects = ["бронь/билет отменится", "напоминания прекратятся"]
        if p.get("has_ticket"):
            effects.append("билет в чате уберётся")
        if notify_enabled and p.get("will_notify"):
            effects.append('<span class="events-impact-msg">получит сообщение</span>')
        elif notify_enabled:
            effects.append('<span class="muted">сообщение не уйдёт (не в выбранной группе)</span>')
        rows.append(
            "<tr>"
            f"<td>#{_h(p.get('booking_id'))}</td>"
            f"<td>{_h(p.get('name'))}<br><span class='muted'>{_h(p.get('channel'))}</span></td>"
            f"<td>{_h(p.get('event_label'))}</td>"
            f"<td>{_h(p.get('status_label'))}</td>"
            f"<td>{' · '.join(effects)}</td>"
            "</tr>"
        )

    notify_line = (
        f"<li>Сообщение получат: <b>{notify_count}</b> чел.</li>"
        if notify_enabled
        else "<li>Сообщение гостям: <b>не отправляется</b> (или нет текста / аудитории).</li>"
    )
    return (
        '<div class="events-impact-box">'
        "<b>⚠ Что произойдёт при «Обновить»</b>"
        "<ul class='events-impact-summary'>"
        f"<li>Шоу к скрытию: <b>{event_count}</b></li>"
        f"<li>Отменятся активные брони/билеты: <b>{len(people)}</b></li>"
        f"<li>Из них с билетом в чате: <b>{ticket_count}</b></li>"
        f"{notify_line}"
        "</ul>"
        '<div class="table-wrap"><table class="events-impact-table"><thead><tr>'
        "<th>Бронь</th><th>Гость</th><th>Шоу</th><th>Сейчас</th><th>Действия</th>"
        f"</tr></thead><tbody>{''.join(rows)}</tbody></table></div>"
        "</div>"
    )
