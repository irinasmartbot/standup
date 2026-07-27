"""VK free-booking flow for Проверка материала."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from bot.config import CHANNEL_LINK, MANAGER_LINK, MANAGER_PHONE
from bot.db.crud import (
    create_booking,
    get_active_booking_by_id,
    get_last_phone,
    get_total_guests,
    save_ticket_message_id,
    update_booking_status,
)
from bot.services.sheets import load_events
from bot.utils.phone import normalize_phone
from bot.utils.ticket import format_date, generate_ticket, guests_word, now_msk
from bot.vk.formatting import format_vk_text
from bot.vk.keyboards import VKKeyboardBuilder


STEP_NAME = "waiting_name"
STEP_PHONE = "waiting_phone"
STEP_GUESTS = "waiting_guests"

PHONE_ASK_TEXT = (
    "Введите номер телефона с кодом страны.\n"
    "Пример: +79001234567"
)
PHONE_INVALID_TEXT = (
    "Нужен корректный номер телефона с кодом страны — без букв и лишних символов.\n"
    "Пример: +79001234567\n\n"
    "Введите номер ещё раз 👇"
)


def _payload(value: str, **extra) -> dict[str, Any]:
    return {"cmd": value, **extra}


def guests_keyboard() -> str:
    kb = VKKeyboardBuilder(inline=True)
    for n in (1, 2, 3, 4):
        kb.button(str(n), _payload("booking_guests", guests=n), color="primary")
    kb.button("Отмена", _payload("booking_cancel"))
    kb.adjust(4, 1)
    return kb.as_json()


def phone_saved_keyboard(phone: str) -> str:
    kb = VKKeyboardBuilder(inline=True)
    kb.button("Да, использовать", _payload("booking_phone_use"), color="primary")
    kb.button("Ввести другой номер", _payload("booking_phone_change"))
    kb.button("Отмена", _payload("booking_cancel"))
    kb.adjust(1)
    return kb.as_json()


def booking_cancel_keyboard() -> str:
    kb = VKKeyboardBuilder(inline=True)
    kb.button("Отмена", _payload("booking_cancel"))
    kb.adjust(1)
    return kb.as_json()


def after_booking_keyboard(booking_id: int, *, offer_ticket: bool) -> str:
    kb = VKKeyboardBuilder(inline=True)
    if offer_ticket:
        kb.button("Получить билет", _payload("booking_get_ticket", booking_id=booking_id), color="primary")
    kb.button("В главное меню", _payload("main_menu"))
    kb.adjust(1)
    return kb.as_json()


def manage_ticket_keyboard(booking_id: int, settings_manager_link: str) -> str:
    kb = VKKeyboardBuilder(inline=True)
    kb.button("Отменить бронь", _payload("mb_cancel_confirm", booking_id=booking_id), color="negative")
    kb.button("Изменить дату", _payload("mb_change_date_confirm", booking_id=booking_id))
    kb.button("Задать вопрос менеджеру", link=settings_manager_link)
    kb.button("В главное меню", _payload("main_menu"))
    kb.adjust(1)
    return kb.as_json()


async def find_event(event_id: Any) -> dict[str, Any] | None:
    return next((e for e in await load_events("proverka") if str(e["id"]) == str(event_id)), None)


def start_session(sessions: dict[int, dict], vk_id: int, event: dict[str, Any]) -> dict:
    session = {
        "step": STEP_NAME,
        "event_id": event["id"],
        "event_date": event["date"],
        "event_time": event["time"],
        "event_address": event.get("address") or "",
        "event_location": event.get("location") or "",
        "weekday": event.get("weekday") or "",
        "max_seats": event.get("max_seats") or 0,
        "name": "",
        "phone": "",
    }
    sessions[vk_id] = session
    return session


def clear_session(sessions: dict[int, dict], vk_id: int) -> None:
    sessions.pop(vk_id, None)


async def complete_booking(
    *,
    client,
    peer_id: int,
    vk_id: int,
    session: dict,
    guests: int,
    manager_link: str,
    community_link: str,
) -> int:
    event_date = session["event_date"]
    event_time = session["event_time"]
    event = await find_event(session["event_id"])
    max_seats = (event or {}).get("max_seats") or session.get("max_seats") or 0
    if max_seats:
        total = get_total_guests(event_date, event_time)
        if total + guests > max_seats:
            available = max_seats - total
            if available <= 0:
                raise RuntimeError("no_seats")
            raise RuntimeError(f"only:{available}")

    booking_id = create_booking(
        None,
        "",
        session.get("name") or "",
        session.get("phone") or "",
        event_date,
        event_time,
        session.get("event_address") or "",
        session.get("event_location") or "",
        guests,
        booking_format="proverka",
        event_format="proverka",
        event_id=session.get("event_id"),
        vk_id=vk_id,
        source="vkontakte",
    )

    try:
        days_until = (datetime.strptime(event_date, "%d.%m.%Y").date() - now_msk().date()).days
    except Exception:
        days_until = 99

    date_str = format_date(event_date)
    weekday = (event or {}).get("weekday") or session.get("weekday") or ""
    weekday_part = f" ({weekday})" if weekday else ""
    address = (event or {}).get("address") or session.get("event_address") or ""
    offer_ticket = days_until <= 1

    if offer_ticket:
        text = (
            f"Отлично!\n\n"
            f"Важная информация — чтобы закрепить место:\n"
            f"Дата: {date_str}{weekday_part}\n"
            f"Время: {event_time}\n\n"
            f"ОБЯЗАТЕЛЬНО подтвердите бронь кнопкой «Получить билет».\n"
            f"Если не успеете подтвердить, бронь будет аннулирована."
        )
    else:
        text = (
            f"Отлично! Мы внесли вас в списки гостей:\n\n"
            f"Дата: {date_str}{weekday_part}\n"
            f"Время: {event_time}\n"
            f"Локация: {address}\n"
            f"Количество гостей: {guests} чел.\n\n"
            f"За сутки до мероприятия придёт напоминание с кнопкой «Получить билет». "
            f"Обязательно нажмите её, чтобы подтвердить бронь."
        )

    await client.send_message(
        peer_id,
        text,
        keyboard=after_booking_keyboard(booking_id, offer_ticket=offer_ticket),
    )
    return booking_id


async def issue_ticket(
    *,
    client,
    peer_id: int,
    booking_id: int,
    manager_link: str,
) -> None:
    booking = get_active_booking_by_id(booking_id)
    if not booking:
        await client.send_message(peer_id, "Бронь не найдена или уже отменена.")
        return
    status = booking[10]
    if status == "confirmed":
        await client.send_message(peer_id, "Билет уже был выдан ранее.")
        return

    name = booking[3]
    event_date = booking[5]
    event_time = booking[6]
    event_address = booking[7] or ""
    event_location = booking[8] or ""
    guests = booking[9]

    events = await load_events("proverka")
    event = next(
        (e for e in events if e.get("date") == event_date and e.get("time") == event_time),
        None,
    )
    if event:
        confirmed_guests = get_total_guests(event_date, event_time, exclude_id=booking_id)
        if confirmed_guests + guests > event["max_seats"]:
            available = max(0, event["max_seats"] - confirmed_guests)
            await client.send_message(
                peer_id,
                "К сожалению, на это мероприятие уже не осталось мест для подтверждения билета.\n\n"
                f"Сейчас свободно: {available}.",
            )
            return

    short_address = (
        f"{event_location}, {event_address.split(',', 1)[1].strip()}"
        if event_location and "," in event_address
        else f"{event_location}, {event_address}".strip(", ")
    )
    place = f"{event_location}, {event_address}".strip(", ") if event_location else event_address
    ticket_buf = generate_ticket(name or "", event_date, event_time, short_address, guests)
    update_booking_status(booking_id, "confirmed")

    caption = format_vk_text(
        "Отлично!\n\n"
        "<b>Данные по билету:</b>\n\n"
        f"<b>Ваше имя:</b> {name or ''}\n"
        f"<b>Дата:</b> {event_date or ''}\n"
        f"<b>Время:</b> {event_time or ''}\n"
        f"<b>Место:</b> {place}\n"
        f"<b>Количество гостей:</b> {guests_word(guests)}\n\n"
        "Ждем вас на мероприятии ❤️\n\n"
        f"При вопросах — менеджеру ({MANAGER_LINK}). "
        f"Если срочно — звоните {MANAGER_PHONE}.\n\n"
        f"Канал анонсов: {CHANNEL_LINK}"
    )
    attachment = await client.upload_message_photo(
        peer_id,
        ticket_buf.getvalue(),
        filename=f"ticket_{booking_id}.jpg",
    )
    msg_id = await client.send_message(
        peer_id,
        caption,
        attachment=attachment,
        keyboard=manage_ticket_keyboard(booking_id, manager_link),
    )
    save_ticket_message_id(booking_id, msg_id)


def saved_phone_for(vk_id: int) -> str | None:
    return normalize_phone(get_last_phone(vk_id=vk_id))
