"""VK «Мои брони»: список, билет, отмена, смена даты/гостей (как /my_bookings в TG)."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from bot.config import CHANNEL_LINK, MANAGER_PHONE
from bot.db.crud import (
    get_active_booking_by_id,
    get_total_guests,
    get_user_bookings_for_commands,
    update_booking_guests,
    update_booking_status,
)
from bot.handlers.start import MY_BOOKINGS_INTRO
from bot.services.sheets import load_events
from bot.utils.ticket import event_already_passed, format_date, format_ticket_place, generate_ticket
from bot.vk.keyboards import VKKeyboardBuilder

STEP_NEW_GUESTS = "waiting_new_guests"


def _payload(value: str, **extra) -> dict[str, Any]:
    return {"cmd": value, **extra}


def _format_label(format_name: str) -> str:
    if format_name == "rozygrysh":
        return "Розыгрыш"
    if format_name == "proverka":
        return "Проверка материала"
    return format_name or ""


def list_rows(vk_id: int) -> list:
    rows = list(get_user_bookings_for_commands(vk_id=vk_id) or [])

    def _sort_key(row):
        try:
            return datetime.strptime(f"{row[3]} {row[4]}", "%d.%m.%Y %H:%M")
        except (TypeError, ValueError, IndexError):
            return datetime.max

    return sorted(rows, key=_sort_key)


def booking_card_text(row, *, page: int = 0, total: int = 1) -> str:
    """Текст карточки — как в Telegram `/my_bookings`."""
    _, format_name, status, event_date, event_time, address, location, guests, *_ = row
    position = f" {page + 1}/{total}" if total > 1 else ""
    title = _format_label(format_name)
    title_line = f"<b>{title}</b>{position}" if position else f"<b>{title}</b>"
    lines = [
        f"<b><i>{MY_BOOKINGS_INTRO}</i></b>",
        "",
        title_line,
        f"📅 <b>{event_date}</b> в <b>{event_time}</b>",
        f"📍 <b>{location or ''}</b>",
        f"<b>Адрес:</b> {address or ''}",
        f"<b>Гостей:</b> {guests}",
    ]
    if status == "confirmed":
        lines.extend(["", "✅ <b>Бронь подтверждена</b>"])
    return "\n".join(lines)


def empty_bookings_text() -> str:
    return f"<b><i>{MY_BOOKINGS_INTRO}</i></b>\n\nАктивных броней пока нет."


def ticket_caption(row) -> str:
    _, format_name, _, event_date, event_time, address, location, *_ = row
    return (
        "<b>Билет по брони</b>\n\n"
        f"{_format_label(format_name)}\n"
        f"📅 {event_date} в {event_time}\n"
        f"📍 {location or ''}\n"
        f"Адрес: {address or ''}"
    )


def ticket_bytes(row) -> bytes:
    booking_id, format_name, _, event_date, event_time, address, location, guests, _, _, name = row
    place = format_ticket_place(location or "", address or "")
    buf = generate_ticket(name or "", event_date, event_time, place, guests)
    return buf.getvalue()


def bookings_keyboard(row, *, page: int = 0, total: int = 1) -> str:
    """Клавиатура карточки — как в Telegram (карусель + билет при confirmed)."""
    booking_id, format_name, status, *_ = row
    kb = VKKeyboardBuilder(inline=True)
    action_count = 0
    is_raffle = format_name == "rozygrysh"

    # «Получить билет» только из напоминания; в карточке — «Билет по брони», если confirmed
    if status == "confirmed":
        kb.button("🎟 Билет по брони", _payload("mb_ticket", page=page), color="primary")
        action_count += 1
        kb.button("Отменить бронь", _payload("mb_cancel_confirm", booking_id=booking_id))
        action_count += 1
        if not is_raffle:
            kb.button("Изменить дату", _payload("mb_change_date_confirm", booking_id=booking_id))
            action_count += 1
    else:
        kb.button("Отменить бронь", _payload("mb_cancel_confirm", booking_id=booking_id))
        action_count += 1
        if not is_raffle:
            kb.button("Изменить дату", _payload("mb_change_date_confirm", booking_id=booking_id))
            kb.button(
                "Изменить количество гостей",
                _payload("mb_change_guests_confirm", booking_id=booking_id),
            )
            action_count += 2
        else:
            # Розыгрыш: 1 гость, дату после брони не меняем (как в TG).
            pass

    nav_count = 0
    if total > 1:
        # Как в TG: на первой — только «Далее», на последней — только «Назад»,
        # в середине — обе стрелки. Пустых кнопок по краям нет.
        if page > 0:
            kb.button("⬅️ Назад", _payload("mb_page", page=page - 1))
            nav_count += 1
        kb.button(f"{page + 1}/{total}", _payload("mb_noop"))
        nav_count += 1
        if page < total - 1:
            kb.button("Далее ➡️", _payload("mb_page", page=page + 1))
            nav_count += 1

    kb.button("⬅️ В главное меню", _payload("main_menu"))
    if total > 1:
        kb.adjust(*([1] * action_count), nav_count, 1)
    else:
        kb.adjust(1)
    return kb.as_json()


def ticket_view_keyboard(page: int = 0) -> str:
    kb = VKKeyboardBuilder(inline=True)
    kb.button("⬅️ Назад к броням", _payload("my_bookings", page=page))
    kb.button("⬅️ В главное меню", _payload("main_menu"))
    kb.adjust(1)
    return kb.as_json()


def confirm_keyboard(cmd: str, booking_id: int) -> str:
    kb = VKKeyboardBuilder(inline=True)
    kb.button("Подтверждаю", _payload(cmd, booking_id=booking_id), color="primary")
    kb.button("⬅️ Назад к броням", _payload("my_bookings"))
    kb.adjust(1)
    return kb.as_json()


def after_cancel_keyboard(community_link: str) -> str:
    kb = VKKeyboardBuilder(inline=True)
    if community_link:
        kb.button("Наше сообщество", link=community_link)
    kb.button("⬅️ В главное меню", _payload("main_menu"))
    kb.adjust(1)
    return kb.as_json()


def change_guests_done_keyboard(booking_id: int, *, offer_ticket: bool = False) -> str:
    kb = VKKeyboardBuilder(inline=True)
    if offer_ticket:
        kb.button(
            "Получить билет",
            _payload("booking_get_ticket", booking_id=booking_id),
            color="primary",
        )
    kb.button("Отменить бронь", _payload("mb_cancel_confirm", booking_id=booking_id))
    kb.button("Изменить дату", _payload("mb_change_date_confirm", booking_id=booking_id))
    kb.button(
        "Изменить количество гостей",
        _payload("mb_change_guests_confirm", booking_id=booking_id),
    )
    kb.button("⬅️ В главное меню", _payload("main_menu"))
    kb.adjust(1)
    return kb.as_json()


def guests_pick_keyboard(booking_id: int) -> str:
    kb = VKKeyboardBuilder(inline=True)
    for n in (1, 2, 3, 4):
        kb.button(str(n), _payload("mb_change_guests_set", booking_id=booking_id, guests=n), color="primary")
    kb.button("⬅️ Назад к броням", _payload("my_bookings"))
    kb.adjust(4, 1)
    return kb.as_json()


def booking_belongs_to_vk(booking_id: int, vk_id: int) -> Any | None:
    """Return command-row if booking is active and owned by this VK user."""
    for row in list_rows(vk_id):
        if int(row[0]) == int(booking_id):
            return row
    return None


def actionable_booking(booking_id: int, vk_id: int) -> tuple[Any | None, str | None]:
    """(booking_full_row, error_message). booking from get_active_booking_by_id."""
    if not booking_belongs_to_vk(booking_id, vk_id):
        return None, "Эта бронь уже отменена или не найдена."
    booking = get_active_booking_by_id(booking_id)
    if not booking:
        return None, "Эта бронь уже отменена или не найдена."
    if event_already_passed(booking[5], booking[6]):
        return None, "past"
    return booking, None


async def delete_ticket_message(client, peer_id: int, booking_id: int) -> None:
    booking = get_active_booking_by_id(booking_id)
    if not booking:
        return
    ticket_msg_id = booking[15] if len(booking) > 15 else None
    if not ticket_msg_id:
        return
    try:
        await client.delete_messages(peer_id, [int(ticket_msg_id)])
    except Exception:
        pass


async def apply_new_guests(*, booking_id: int, guests: int) -> tuple[bool, str]:
    from bot.db.crud import get_booking_format

    if (get_booking_format(booking_id) or "").strip().lower() == "rozygrysh":
        return False, "В розыгрыше бронь только на 1 гостя — изменить количество нельзя."
    booking = get_active_booking_by_id(booking_id)
    if not booking:
        return False, "Бронь не найдена."
    if guests < 1 or guests > 4:
        return False, "Максимум 4 человека. Выберите число от 1 до 4."

    events = await load_events("proverka")
    event = next(
        (e for e in events if e.get("date") == booking[5] and e.get("time") == booking[6]),
        None,
    )
    if event:
        total = get_total_guests(booking[5], booking[6], exclude_id=booking_id)
        available = max(0, int(event.get("max_seats") or 0) - total)
        if guests > available:
            return False, f"К сожалению, доступно только {available} мест. Укажите меньшее количество."

    update_booking_guests(booking_id, guests)
    date_str = format_date(booking[5])
    return (
        True,
        f"Спасибо, количество гостей изменено на {guests}. Будем ждать Вас {date_str} 👍\n\n"
        f"При возникновении вопросов — можно писать менеджеру (если срочно — звоните {MANAGER_PHONE})",
    )


def cancel_done_text(*, community_link: str = "", manager_link: str = "") -> str:
    community = (community_link or "").strip() or CHANNEL_LINK
    manager = (manager_link or "").strip() or "@ccoverr"
    return (
        "Хорошо, спасибо, что предупредили 😊 Ждём Вас на других мероприятиях, "
        "актуальная афиша всегда на нашем сайте: MoscowStandUpshow.ru\n\n"
        f"При возникновении вопросов - можно писать менеджеру ({manager})\n\n"
        f"И не забудь заглянуть в наше сообщество ({community}) "
        "(там часто дарят бесплатные билеты на платные шоу 😉)"
    )


def clear_manage_session(sessions: dict[int, dict], vk_id: int) -> None:
    sessions.pop(vk_id, None)
