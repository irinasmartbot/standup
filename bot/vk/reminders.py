"""VK reminders / annulment for proverka bookings (source=vkontakte)."""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta

from bot.config import CHANNEL_LINK
from bot.db.crud import (
    annul_booking,
    get_booked_for_reminders,
    save_confirm_message_id,
    update_reminder_flag,
)
from bot.utils.ticket import format_date, now_msk, parse_created_at, parse_event_datetime
from bot.vk.client import VKClient
from bot.vk.config import load_vk_settings
from bot.vk.keyboards import VKKeyboardBuilder

logger = logging.getLogger(__name__)


def _payload(value: str, **extra) -> dict:
    return {"cmd": value, **extra}


def _reminder_keyboard(booking_id: int) -> str:
    kb = VKKeyboardBuilder(inline=True)
    kb.button("Получить билет", _payload("booking_get_ticket", booking_id=booking_id), color="primary")
    kb.button("Отменить бронь", _payload("mb_cancel_confirm", booking_id=booking_id))
    kb.button("Изменить дату", _payload("mb_change_date_confirm", booking_id=booking_id))
    kb.button(
        "Изменить количество гостей",
        _payload("mb_change_guests_confirm", booking_id=booking_id),
    )
    kb.button("В главное меню", _payload("main_menu"))
    kb.adjust(1)
    return kb.as_json()


def _annul_keyboard(community_link: str) -> str:
    kb = VKKeyboardBuilder(inline=True)
    if community_link:
        kb.button("Наше сообщество", link=community_link)
    kb.button("В главное меню", _payload("main_menu"))
    kb.adjust(1)
    return kb.as_json()


def _row_channel(row) -> str:
    source = ""
    vk_id = None
    telegram_id = row[1]
    if len(row) > 12:
        vk_id = row[11]
        source = (row[12] or "").strip()
    elif len(row) > 11:
        vk_id = row[11]
    if source in {"telegram", "vkontakte"}:
        return source
    if telegram_id and not vk_id:
        return "telegram"
    if vk_id and not telegram_id:
        return "vkontakte"
    if source:
        return source
    return "telegram" if telegram_id else ("vkontakte" if vk_id else "unknown")


async def send_vk_booking_reminder(client: VKClient, row, reminder_type: str, *, community_link: str) -> None:
    booking_id = row[0]
    name = row[2]
    event_date = row[3]
    event_time = row[4]
    event_address = row[5]
    guests = row[7]
    vk_id = row[11] if len(row) > 11 else None
    if not vk_id:
        raise RuntimeError(f"VK reminder skipped: booking {booking_id} has no vk_id")

    date_str = format_date(event_date)
    if reminder_type == "day":
        text = (
            f"{name}, мне необходимо подтвердить, либо отменить Вашу бронь "
            f"на {date_str} в {event_time} 😊\n\n"
            f"Чтобы подтвердить бронь, нажми на «Получить билет» 👇"
        )
    else:
        text = (
            f"Напоминание о брони на Moscow StandUp Show:\n\n"
            f"Дата: {date_str}\n"
            f"Время: {event_time}\n"
            f"Адрес: {event_address}\n"
            f"Количество гостей: {guests} чел.\n\n"
            f"Сбор гостей начинается за полчаса до начала шоу. "
            f"Для подтверждения брони нажмите «Получить билет» 👇"
        )

    msg_id = await client.send_message(
        int(vk_id),
        text,
        keyboard=_reminder_keyboard(booking_id),
    )
    save_confirm_message_id(booking_id, msg_id)


async def send_vk_annulled_message(client: VKClient, row, *, community_link: str, manager_link: str) -> None:
    booking_id = row[0]
    vk_id = row[11] if len(row) > 11 else None
    if not vk_id:
        # Still annul in DB so the booking does not hang forever.
        annul_booking(booking_id)
        raise RuntimeError(f"VK annul without vk_id for booking {booking_id}")

    link = community_link or CHANNEL_LINK
    text = (
        "Ваша бронь аннулирована, ждём Вас на других мероприятиях 😊\n\n"
        f"При возникновении вопросов можно писать менеджеру: {manager_link}\n\n"
        f"И не забудь заглянуть в наше сообщество: {link}"
    )
    await client.send_message(
        int(vk_id),
        text,
        keyboard=_annul_keyboard(community_link),
    )
    annul_booking(booking_id)


async def process_due_vk_reminders(client: VKClient, *, community_link: str, manager_link: str) -> None:
    now = now_msk().replace(tzinfo=None)
    for row in get_booked_for_reminders("proverka"):
        if _row_channel(row) != "vkontakte":
            continue
        booking_id = row[0]
        event_date = row[3]
        event_time = row[4]
        created_at = parse_created_at(row[8])
        reminder_24h_sent = bool(row[9])
        reminder_day_sent = bool(row[10])
        event_dt = parse_event_datetime(event_date, event_time)
        if not event_dt:
            logger.warning("VK reminder: bad datetime booking=%s %s %s", booking_id, event_date, event_time)
            continue

        one_day_reminder_at = datetime.combine(
            event_dt.date() - timedelta(days=1), datetime.min.time()
        ).replace(hour=14)
        ten_am_on_event_day = datetime.combine(event_dt.date(), datetime.min.time()).replace(hour=10)
        time_until_at_booking = event_dt - created_at

        if created_at < ten_am_on_event_day:
            day_fire_at = ten_am_on_event_day
        elif time_until_at_booking >= timedelta(hours=2):
            day_fire_at = created_at + timedelta(hours=2)
        elif time_until_at_booking >= timedelta(hours=1):
            day_fire_at = event_dt - timedelta(hours=1)
        elif time_until_at_booking >= timedelta(minutes=30):
            day_fire_at = created_at + timedelta(minutes=15)
        elif time_until_at_booking >= timedelta(minutes=10):
            day_fire_at = created_at + timedelta(minutes=1)
        else:
            day_fire_at = None

        if created_at >= event_dt - timedelta(hours=2):
            annul_at = event_dt + timedelta(minutes=30)
        else:
            annul_at = event_dt - timedelta(hours=2)

        try:
            days_before_event = (event_dt.date() - created_at.date()).days
            if (
                not reminder_24h_sent
                and days_before_event >= 2
                and now >= one_day_reminder_at
                and now < event_dt
            ):
                await send_vk_booking_reminder(client, row, "24h", community_link=community_link)
                update_reminder_flag(booking_id, "reminder_24h_sent")

            if (
                not reminder_day_sent
                and day_fire_at is not None
                and now >= day_fire_at
                and now < event_dt
            ):
                await send_vk_booking_reminder(client, row, "day", community_link=community_link)
                update_reminder_flag(booking_id, "reminder_day_sent")

            if now >= annul_at:
                await send_vk_annulled_message(
                    client,
                    row,
                    community_link=community_link,
                    manager_link=manager_link,
                )
        except Exception:
            logger.exception("Failed VK reminder for booking %s", booking_id)


async def vk_reminder_loop(client: VKClient, *, community_link: str, manager_link: str) -> None:
    while True:
        try:
            await process_due_vk_reminders(
                client,
                community_link=community_link,
                manager_link=manager_link,
            )
        except Exception:
            logger.exception("VK reminder loop iteration failed")
        await asyncio.sleep(60)
