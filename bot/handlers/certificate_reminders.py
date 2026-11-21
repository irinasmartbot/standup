"""Напоминания брони по сертификату: кнопка подтверждает бронь, билета нет."""

from __future__ import annotations

import asyncio
import logging

from aiogram.utils.keyboard import InlineKeyboardBuilder

from bot.config import bot
from bot.db.crud import annul_booking, get_booked_for_reminders, update_reminder_flag
from bot.services.cert_flow import expire_booking
from bot.services.cert_texts import CONFIRM_BUTTON, reminder_24h_text, reminder_day_text
from bot.utils.show_formats import CERTIFICATE
from bot.utils.ticket import format_date, now_msk, parse_created_at, parse_event_datetime

logger = logging.getLogger(__name__)


def _kb(booking_id: int):
    kb = InlineKeyboardBuilder()
    kb.button(text=CONFIRM_BUTTON, callback_data=f"cert_cf:{int(booking_id)}", style="success")
    kb.button(text="Отменить бронь", callback_data=f"cert_cx:{int(booking_id)}")
    kb.button(text="Перенести дату", callback_data=f"cert_mv:{int(booking_id)}")
    kb.adjust(1)
    return kb.as_markup()


async def send_certificate_reminder(row, reminder_type: str) -> None:
    booking_id = row[0]
    telegram_id = row[1]
    name = row[2]
    event_date = row[3]
    event_time = row[4]
    event_address = row[5] if len(row) > 5 else ""
    event_location = row[6] if len(row) > 6 else ""
    if not telegram_id:
        return
    date_str = format_date(event_date)
    location_line = f"📍 Локация {event_location}, {event_address}".strip(", ")
    if reminder_type == "day":
        text = reminder_day_text(name=name, date_str=date_str, event_time=event_time)
    else:
        text = reminder_24h_text(
            name=name,
            event_time=event_time,
            location_line=location_line,
        )
    await bot.send_message(
        int(telegram_id),
        text,
        reply_markup=_kb(int(booking_id)),
        parse_mode="HTML",
    )


async def send_certificate_annulled(row) -> None:
    telegram_id = row[1]
    if telegram_id:
        await bot.send_message(
            int(telegram_id),
            "Бронь по сертификату снята: подтверждение не пришло вовремя. "
            "Номер можно использовать на другую дату.",
        )


async def process_certificate_reminders() -> None:
    from bot.utils.reminder_schedule import plan_booking_reminders

    now = now_msk().replace(tzinfo=None)
    for row in get_booked_for_reminders(CERTIFICATE):
        source = (row[12] if len(row) > 12 else "") or ""
        if source == "vkontakte":
            continue
        booking_id = row[0]
        event_dt = parse_event_datetime(row[3], row[4])
        if not event_dt:
            continue
        created_at = parse_created_at(row[8])
        plan = plan_booking_reminders(created_at, event_dt)
        try:
            if (
                not bool(row[9])
                and plan["reminder_24h_at"] is not None
                and now >= plan["reminder_24h_at"]
                and now < event_dt
            ):
                await send_certificate_reminder(row, "24h")
                update_reminder_flag(booking_id, "reminder_24h_sent")
            if (
                not bool(row[10])
                and plan["reminder_day_at"] is not None
                and now >= plan["reminder_day_at"]
                and now < event_dt
            ):
                await send_certificate_reminder(row, "day")
                update_reminder_flag(booking_id, "reminder_day_sent")
            if now >= plan["annul_at"]:
                outcome = await expire_booking(int(booking_id))
                if outcome != "ok":
                    continue
                await send_certificate_annulled(row)
                annul_booking(booking_id)
        except Exception:
            logger.exception("certificate reminder failed booking=%s", booking_id)


async def certificate_reminder_loop() -> None:
    while True:
        await process_certificate_reminders()
        await asyncio.sleep(60)
