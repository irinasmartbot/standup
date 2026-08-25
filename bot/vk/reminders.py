"""VK reminders / annulment for proverka and rozygrysh bookings (source=vkontakte)."""

from __future__ import annotations

import asyncio
import logging

from bot.config import CHANNEL_LINK
from bot.db.crud import (
    annul_booking,
    clear_raffle_after_user_cancel,
    get_booked_for_reminders,
    save_confirm_message_id,
    update_reminder_flag,
)
from bot.utils.booking_texts import proverka_reminder_24h_text, raffle_reminder_24h_text
from bot.utils.ticket import format_date, now_msk, parse_created_at, parse_event_datetime
from bot.vk.client import VKClient
from bot.vk.keyboards import VKKeyboardBuilder

logger = logging.getLogger(__name__)


def _payload(value: str, **extra) -> dict:
    return {"cmd": value, **extra}


def _reminder_keyboard(booking_id: int, *, raffle: bool = False) -> str:
    kb = VKKeyboardBuilder(inline=True)
    kb.button(
        "Получить билет",
        _payload("booking_get_ticket", booking_id=booking_id),
        color="primary",
    )
    kb.button("Отменить бронь", _payload("mb_cancel_confirm", booking_id=booking_id))
    if not raffle:
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


async def send_vk_booking_reminder(
    client: VKClient,
    row,
    reminder_type: str,
    *,
    community_link: str,
    raffle: bool = False,
) -> None:
    booking_id = row[0]
    name = row[2]
    event_date = row[3]
    event_time = row[4]
    event_address = row[5]
    event_location = row[6]
    guests = row[7]
    vk_id = row[11] if len(row) > 11 else None
    if not vk_id:
        raise RuntimeError(f"VK reminder skipped: booking {booking_id} has no vk_id")

    date_str = format_date(event_date)
    if reminder_type == "day":
        text = (
            f"{name}, мне необходимо подтвердить, либо отменить Вашу бронь "
            f"на <b>{date_str}</b> в <b>{event_time}</b> 😊\n\n"
            f"Чтобы подтвердить бронь, нажми на <b>«Получить билет»</b> 👇"
        )
    elif raffle:
        location_line = f"📍 Локация {event_location}, {event_address}".strip(", ")
        text = raffle_reminder_24h_text(
            event_time=event_time,
            location_line=location_line,
            guests=guests or 1,
            expandable=False,
        )
    else:
        address_line = (event_address or "").strip()
        if event_location:
            address_line = f"{event_location}, {address_line}".strip(", ")
        text = proverka_reminder_24h_text(
            date_str=date_str,
            event_time=event_time,
            address_line=address_line,
            guests=guests or 1,
            expandable=False,
        )

    msg_id = await client.send_message(
        int(vk_id),
        text,
        keyboard=_reminder_keyboard(booking_id, raffle=raffle),
    )
    save_confirm_message_id(booking_id, msg_id)


async def send_vk_annulled_message(
    client: VKClient,
    row,
    *,
    community_link: str,
    manager_link: str,
    raffle: bool = False,
) -> None:
    booking_id = row[0]
    vk_id = row[11] if len(row) > 11 else None
    if not vk_id:
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
    if raffle:
        try:
            clear_raffle_after_user_cancel(vk_id=int(vk_id), reason="booking_annulled")
        except Exception:
            logger.exception("clear raffle entitlement on annul failed booking=%s", booking_id)


async def _process_format_reminders(
    client: VKClient,
    *,
    booking_format: str,
    community_link: str,
    manager_link: str,
) -> None:
    from bot.utils.reminder_schedule import plan_booking_reminders

    raffle = booking_format == "rozygrysh"
    now = now_msk().replace(tzinfo=None)
    for row in get_booked_for_reminders(booking_format):
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
            logger.warning(
                "VK reminder: bad datetime booking=%s %s %s",
                booking_id,
                event_date,
                event_time,
            )
            continue

        plan = plan_booking_reminders(created_at, event_dt)
        day_fire_at = plan["reminder_day_at"]
        annul_at = plan["annul_at"]
        one_day_reminder_at = plan["reminder_24h_at"]

        try:
            if (
                not reminder_24h_sent
                and one_day_reminder_at is not None
                and now >= one_day_reminder_at
                and now < event_dt
            ):
                await send_vk_booking_reminder(
                    client,
                    row,
                    "24h",
                    community_link=community_link,
                    raffle=raffle,
                )
                update_reminder_flag(booking_id, "reminder_24h_sent")
                try:
                    from bot.db.analytics import EVENT_BOOKING_REMINDER_24H, track_event

                    track_event(
                        EVENT_BOOKING_REMINDER_24H,
                        vk_id=int(row[11]),
                        channel="vkontakte",
                        booking_id=int(booking_id),
                        props={"format": booking_format},
                    )
                except Exception:
                    pass

            if (
                not reminder_day_sent
                and day_fire_at is not None
                and now >= day_fire_at
                and now < event_dt
            ):
                await send_vk_booking_reminder(
                    client,
                    row,
                    "day",
                    community_link=community_link,
                    raffle=raffle,
                )
                update_reminder_flag(booking_id, "reminder_day_sent")
                try:
                    from bot.db.analytics import EVENT_BOOKING_REMINDER_DAY, track_event

                    track_event(
                        EVENT_BOOKING_REMINDER_DAY,
                        vk_id=int(row[11]),
                        channel="vkontakte",
                        booking_id=int(booking_id),
                        props={"format": booking_format},
                    )
                except Exception:
                    pass

            if now >= annul_at:
                await send_vk_annulled_message(
                    client,
                    row,
                    community_link=community_link,
                    manager_link=manager_link,
                    raffle=raffle,
                )
        except Exception:
            logger.exception("Failed VK reminder for booking %s", booking_id)


async def process_due_vk_reminders(client: VKClient, *, community_link: str, manager_link: str) -> None:
    await _process_format_reminders(
        client,
        booking_format="proverka",
        community_link=community_link,
        manager_link=manager_link,
    )
    await _process_format_reminders(
        client,
        booking_format="rozygrysh",
        community_link=community_link,
        manager_link=manager_link,
    )


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
