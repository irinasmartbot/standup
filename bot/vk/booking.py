"""VK free-booking flow for Проверка материала."""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

from bot.config import CHANNEL_LINK, MANAGER_LINK, MANAGER_PHONE
from bot.db.crud import (
    create_booking,
    get_active_booking_by_id,
    get_booking,
    get_last_phone,
    get_total_guests,
    save_ticket_message_id,
    update_booking_status,
)
from bot.services.sheets import load_events
from bot.utils.phone import normalize_phone
from bot.utils.ticket import format_date, generate_ticket, guests_word, now_msk
from bot.vk.keyboards import VKKeyboardBuilder

logger = logging.getLogger(__name__)

STEP_NAME = "waiting_name"
STEP_PHONE = "waiting_phone"
STEP_GUESTS = "waiting_guests"

NAME_ASK_TEXT = "Напишите, пожалуйста, <b>ваше имя</b>."
PHONE_ASK_TEXT = (
    "Введите <b>номер телефона</b> с кодом страны.\n"
    "Пример: <b>+79001234567</b>"
)
PHONE_INVALID_TEXT = (
    "Нужен корректный номер телефона с кодом страны — без букв и лишних символов.\n"
    "Пример: <b>+79001234567</b>\n\n"
    "Введите номер ещё раз 👇"
)


def _payload(value: str, **extra) -> dict[str, Any]:
    return {"cmd": value, **extra}


def name_confirm_text(name: str) -> str:
    safe = (name or "").strip() or "Гость"
    return (
        "Для бронирования вам нужно заполнить некоторые данные\n\n"
        f"Ваше имя <b>{safe}</b>, верно?"
    )


def booking_details_block(
    *,
    date_str: str,
    event_time: str,
    address: str = "",
    guests: int | None = None,
    weekday_part: str = "",
) -> str:
    lines = [
        f"<b>Дата:</b> {date_str}{weekday_part}",
        f"<b>Время:</b> {event_time}",
    ]
    if address:
        lines.append(f"<b>Локация:</b> {address}")
    if guests is not None:
        lines.append(f"<b>Количество гостей:</b> {guests} чел.")
    return "\n".join(lines)


def name_confirm_keyboard(name: str = "") -> str:
    kb = VKKeyboardBuilder(inline=True)
    # В один столбец: в ряду из 2 VK обрезает длинный текст до «С»/эмодзи.
    payload = _payload("booking_name_ok")
    if (name or "").strip():
        payload["name"] = (name or "").strip()[:80]
    kb.button("Да, всё верно", payload, color="primary")
    kb.button("Изменить", _payload("booking_name_change"))
    kb.adjust(1)
    return kb.as_json()


def guests_keyboard() -> str:
    kb = VKKeyboardBuilder(inline=True)
    for n in (1, 2, 3, 4):
        kb.button(str(n), _payload("booking_guests", guests=n), color="primary")
    kb.adjust(4)
    return kb.as_json()


def phone_saved_keyboard(phone: str) -> str:
    kb = VKKeyboardBuilder(inline=True)
    kb.button("Да, использовать", _payload("booking_phone_use"), color="primary")
    kb.button("Ввести другой номер", _payload("booking_phone_change"))
    kb.adjust(1)
    return kb.as_json()


def booking_cancel_keyboard() -> str:
    # Оставлено для совместимости; на шагах имя/телефон больше не показываем.
    kb = VKKeyboardBuilder(inline=True)
    kb.button("В главное меню", _payload("booking_cancel"))
    kb.adjust(1)
    return kb.as_json()


def _adjust_within_vk_rows(kb: VKKeyboardBuilder, *, pair_last_links: bool) -> None:
    """VK inline: max 6 rows. If 7+ buttons, pair link buttons on one row."""
    n = kb.total_buttons
    if n <= 6:
        kb.adjust(1)
        return
    if pair_last_links and n >= 3:
        # …singles, [link|link], menu
        kb.adjust(*([1] * (n - 3) + [2, 1]))
        return
    kb.adjust(*([1] * (n - 2) + [2]))


def after_booking_keyboard(
    booking_id: int,
    *,
    offer_ticket: bool,
    manager_link: str = "",
    community_link: str = "",
) -> str:
    """Как в TG после брони Проверки: билет / отмена / дата / гости / менеджер / сообщество."""
    kb = VKKeyboardBuilder(inline=True)
    if offer_ticket:
        kb.button(
            "🎟 Получить билет",
            _payload("booking_get_ticket", booking_id=booking_id),
            color="positive",
        )
    kb.button("Отменить бронь", _payload("mb_cancel_confirm", booking_id=booking_id))
    kb.button("Изменить дату", _payload("mb_change_date_confirm", booking_id=booking_id))
    kb.button(
        "Изменить количество гостей",
        _payload("mb_change_guests_confirm", booking_id=booking_id),
    )
    has_manager = bool((manager_link or "").strip())
    has_community = bool((community_link or "").strip())
    # Короче в паре — иначе подписи обрежутся в одном ряду.
    pair_links = has_manager and has_community
    if has_manager:
        label = "💬 Менеджеру" if pair_links else "💬 Задать вопрос менеджеру"
        kb.button(label, link=manager_link.strip())
    if has_community:
        label = "📢 Сообщество" if pair_links else "📢 Заглянуть в наше сообщество"
        kb.button(label, link=community_link.strip())
    kb.button("В главное меню", _payload("main_menu"))
    _adjust_within_vk_rows(kb, pair_last_links=pair_links)
    return kb.as_json()


def after_raffle_booking_keyboard(
    booking_id: int,
    *,
    offer_ticket: bool,
    manager_link: str,
    community_link: str,
) -> str:
    """Как в TG после брони розыгрыша: билет / не один / отмена / менеджер / сообщество."""
    kb = VKKeyboardBuilder(inline=True)
    if offer_ticket:
        kb.button(
            "🎟 Получить билет",
            _payload("booking_get_ticket", booking_id=booking_id),
            color="positive",
        )
    kb.button("Что, если я хочу прийти не один?", _payload("rz_not_alone"))
    kb.button("Отменить бронь", _payload("mb_cancel_confirm", booking_id=booking_id))
    has_manager = bool((manager_link or "").strip())
    has_community = bool((community_link or "").strip())
    pair_links = has_manager and has_community
    if has_manager:
        label = "Менеджеру" if pair_links else "Задать вопрос менеджеру"
        kb.button(label, link=manager_link.strip())
    if has_community:
        label = "Сообщество" if pair_links else "Заглянуть в наше сообщество"
        kb.button(label, link=community_link.strip())
    kb.button("В главное меню", _payload("main_menu"))
    _adjust_within_vk_rows(kb, pair_last_links=pair_links)
    return kb.as_json()


def raffle_not_alone_text(*, manager_link: str, paid_booking_link: str = "") -> str:
    manager = (manager_link or "").strip() or MANAGER_LINK
    paid = (paid_booking_link or "").strip()
    if paid:
        booking_part = f'<a href="{paid}">систему бронирования</a>'
    else:
        # Пока нет боевой ссылки — ведём в платную ветку BEST внутри VK-бота.
        booking_part = "систему бронирования (кнопка ниже — StandUp BEST)"
    return (
        f"Ваши друзья могут купить билеты на выбранное Вами шоу через {booking_part}.\n\n"
        f"После этого просто напишите нашему <a href=\"{manager}\">менеджеру</a>, "
        f"на какие места и на какую дату они взяли билеты.\n\n"
        "Мы уберём из продажи соседнее место специально для Вас и посадим туда 😉"
    )


def raffle_not_alone_keyboard(*, paid_booking_link: str = "") -> str:
    kb = VKKeyboardBuilder(inline=True)
    paid = (paid_booking_link or "").strip()
    if paid:
        kb.button("Купить билет друзьям", link=paid, color="positive")
    else:
        kb.button("Купить билет (BEST)", _payload("best"), color="positive")
    kb.adjust(1)
    return kb.as_json()


def manage_ticket_keyboard(booking_id: int, settings_manager_link: str) -> str:
    kb = VKKeyboardBuilder(inline=True)
    kb.button("Отменить бронь", _payload("mb_cancel_confirm", booking_id=booking_id))
    kb.button("Изменить дату", _payload("mb_change_date_confirm", booking_id=booking_id))
    kb.button("Задать вопрос менеджеру", link=settings_manager_link)
    kb.button("В главное меню", _payload("main_menu"))
    kb.adjust(1)
    return kb.as_json()


def already_booked_keyboard(booking_id: int) -> str:
    """Уже есть бронь на этот слот: отмена / смена даты / сразу список дат Проверки."""
    kb = VKKeyboardBuilder(inline=True)
    kb.button("Отменить бронь", _payload("mb_cancel_confirm", booking_id=booking_id))
    kb.button("Изменить дату", _payload("mb_change_date_confirm", booking_id=booking_id))
    # Как в TG: сразу даты, без шага «по дате / по площадке».
    kb.button("Выбрать другую", _payload("check_date_page"), color="primary")
    kb.button("В главное меню", _payload("main_menu"))
    kb.adjust(1)
    return kb.as_json()


def raffle_ticket_manage_keyboard(
    booking_id: int,
    *,
    manager_link: str = "",
) -> str:
    """После выдачи билета розыгрыша — как в TG."""
    kb = VKKeyboardBuilder(inline=True)
    kb.button("Отменить бронь", _payload("mb_cancel_confirm", booking_id=booking_id))
    kb.button("Что, если я хочу прийти не один?", _payload("rz_not_alone"))
    if (manager_link or "").strip():
        kb.button("Задать вопрос менеджеру", link=manager_link.strip())
    kb.button("В главное меню", _payload("main_menu"))
    kb.adjust(1)
    return kb.as_json()


async def find_event(event_id: Any) -> dict[str, Any] | None:
    for fmt in ("proverka", "best"):
        found = next(
            (e for e in await load_events(fmt) if str(e["id"]) == str(event_id)),
            None,
        )
        if found:
            return found
    return None


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
    if get_booking(None, event_date, event_time, vk_id=vk_id):
        raise RuntimeError("already_booked")
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
        booking_format=session.get("booking_format") or "proverka",
        event_format=session.get("event_format") or "proverka",
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

    is_raffle = (session.get("booking_format") or "") == "rozygrysh"
    details = booking_details_block(
        date_str=date_str,
        event_time=event_time or "",
        address=address if not offer_ticket else "",
        guests=None if offer_ticket else guests,
        weekday_part=weekday_part,
    )
    if is_raffle:
        if offer_ticket:
            text = (
                f"<b>Отлично!</b>\n\n"
                f"❗ <b>Важная информация</b> — для того чтобы мы окончательно закрепили за Вами место "
                f"на дату и время:\n"
                f"{details}\n\n"
                f"<b>ОБЯЗАТЕЛЬНО подтвердите бронь, нажав на кнопку «Получить билет»</b>\n\n"
                f"❗ Внимание, если Вы не успеете подтвердить бронь, она будет аннулирована."
            )
        else:
            text = (
                f"<b>Отлично!</b> Мы внесли Вас в списки гостей:\n\n"
                f"{details}\n\n"
                f"<b>❗ Внимание, за сутки до мероприятия Вам придёт сообщение-напоминание "
                f"с подробностями и кнопкой «Получить билет». "
                f"Обязательно нажмите кнопку, чтобы подтвердить бронь. "
                f"Если Вы не успеете подтвердить бронь, она будет аннулирована.</b>\n\n"
                f"Если поменяются планы, обязательно предупредите 😊"
            )
        keyboard = after_raffle_booking_keyboard(
            booking_id,
            offer_ticket=offer_ticket,
            manager_link=manager_link,
            community_link=community_link,
        )
    else:
        # Проверка материала — текст и кнопки как в TG.
        if offer_ticket:
            text = (
                f"<b>Отлично!</b>\n\n"
                f"❗ <b>Важная информация</b> — для того чтобы мы окончательно закрепили за Вами место "
                f"на дату и время:\n"
                f"{details}\n\n"
                f"<b>ОБЯЗАТЕЛЬНО подтвердите бронь, нажав на кнопку «Получить билет»</b>\n\n"
                f"❗ Внимание, если Вы не успеете подтвердить бронь, она будет аннулирована."
            )
        else:
            text = (
                f"<b>Отлично!</b> Мы внесли Вас в списки гостей:\n\n"
                f"{details}\n\n"
                f"<b>❗ Внимание, за сутки до мероприятия Вам придёт сообщение-напоминание "
                f"с подробностями и кнопкой «Получить билет». "
                f"Обязательно нажмите кнопку, чтобы подтвердить бронь. "
                f"Если Вы не успеете подтвердить бронь, она будет аннулирована.</b>\n\n"
                f"Если поменяются планы, обязательно предупредите 😊"
            )
        keyboard = after_booking_keyboard(
            booking_id,
            offer_ticket=offer_ticket,
            manager_link=manager_link,
            community_link=community_link,
        )

    try:
        await client.send_message(peer_id, text, keyboard=keyboard)
    except Exception:
        # Бронь уже в БД — не роняем весь complete_booking (иначе «не удалось создать»).
        logger.exception(
            "VK after-booking message failed booking_id=%s peer_id=%s",
            booking_id,
            peer_id,
        )
        fallback = VKKeyboardBuilder(inline=True)
        if offer_ticket:
            fallback.button(
                "🎟 Получить билет",
                _payload("booking_get_ticket", booking_id=booking_id),
                color="positive",
            )
        fallback.button("В главное меню", _payload("main_menu"))
        fallback.adjust(1)
        await client.send_message(
            peer_id,
            text,
            keyboard=fallback.as_json(),
        )
    return booking_id


async def issue_ticket(
    *,
    client,
    peer_id: int,
    booking_id: int,
    manager_link: str,
    community_link: str = "",
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
    if not event:
        events = await load_events("best")
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
    try:
        from bot.db.crud import get_active_raffle_booking, set_rozygrysh_used

        if get_active_raffle_booking(vk_id=int(peer_id)):
            set_rozygrysh_used(vk_id=int(peer_id), used=True)
    except Exception:
        pass

    vk_manager = (manager_link or "").strip() or MANAGER_LINK
    vk_community = (community_link or "").strip() or CHANNEL_LINK
    is_raffle = False
    try:
        from bot.db.crud import get_active_raffle_booking

        raffle_row = get_active_raffle_booking(vk_id=int(peer_id))
        is_raffle = bool(raffle_row and int(raffle_row[0]) == int(booking_id))
    except Exception:
        pass

    if is_raffle:
        caption = (
            "<b>Отлично!</b>\n\n"
            "<b>Данные по билету:</b>\n\n"
            f"<b>Ваше имя:</b> {name or ''}\n"
            f"<b>Дата:</b> {event_date or ''}\n"
            f"<b>Время:</b> {event_time or ''}\n"
            f"<b>Место:</b> {place}\n"
            f"<b>Количество гостей:</b> {guests_word(guests)}\n\n"
            "Ждем вас на мероприятии ❤️\n\n"
            "❗ <b>ВНИМАНИЕ, ваш билет на одного человека</b>, если вы хотите пойти с друзьями, "
            "чтобы вас посадили вместе — нажмите кнопку «Что, если я хочу прийти не один?» "
            "и узнайте информацию.\n\n"
            f"При вопросах — менеджеру ({vk_manager}). "
            f"Если срочно — звоните {MANAGER_PHONE}.\n\n"
            f"Наше сообщество: {vk_community}"
        )
        keyboard = raffle_ticket_manage_keyboard(booking_id, manager_link=manager_link)
    else:
        caption = (
            "<b>Отлично!</b>\n\n"
            "<b>Данные по билету:</b>\n\n"
            f"<b>Ваше имя:</b> {name or ''}\n"
            f"<b>Дата:</b> {event_date or ''}\n"
            f"<b>Время:</b> {event_time or ''}\n"
            f"<b>Место:</b> {place}\n"
            f"<b>Количество гостей:</b> {guests_word(guests)}\n\n"
            "Ждем вас на мероприятии ❤️\n\n"
            f"При вопросах — менеджеру ({vk_manager}). "
            f"Если срочно — звоните {MANAGER_PHONE}.\n\n"
            f"Канал анонсов: {vk_community}"
        )
        keyboard = manage_ticket_keyboard(booking_id, manager_link)

    attachment = await client.upload_message_photo(
        peer_id,
        ticket_buf.getvalue(),
        filename=f"ticket_{booking_id}.jpg",
    )
    msg_id = await client.send_message(
        peer_id,
        caption,
        attachment=attachment,
        keyboard=keyboard,
    )
    save_ticket_message_id(booking_id, msg_id)


def saved_phone_for(vk_id: int) -> str | None:
    return normalize_phone(get_last_phone(vk_id=vk_id))
