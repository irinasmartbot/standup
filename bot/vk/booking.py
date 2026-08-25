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
    save_confirm_message_id,
    save_ticket_message_id,
    update_booking_status,
)
from bot.services.sheets import load_events
from bot.utils.phone import normalize_phone
from bot.utils.ticket import (
    event_already_passed,
    format_date,
    format_ticket_place,
    generate_ticket,
    guests_word,
    now_msk,
)
from bot.vk.keyboards import VKKeyboardBuilder, empty_inline_keyboard

logger = logging.getLogger(__name__)


async def clear_inline_keyboard(
    client,
    peer_id: int,
    message_ref: int | None = None,
    *,
    conversation_message_id: int | None = None,
) -> bool:
    """Снять inline-кнопки, текст сообщения не трогать.

    Пробуем все переданные id (клик + сохранённый confirm/ticket):
    messages.send в разных версиях API отдаёт message_id или cmid.
    Не останавливаемся на первом успехе — иначе снимается только
    диалог «подтвердите отмену», а кнопки на карточке брони остаются.

    Edit идёт через edit_keyboard_only: с тем же текстом сообщения
    (edit только с keyboard у VK часто не срабатывает).
    """
    kb = empty_inline_keyboard()
    refs: list[tuple[str, int]] = []
    seen: set[tuple[str, int]] = set()

    def _add(kind: str, value: int | None) -> None:
        if not value:
            return
        item = (kind, int(value))
        if item in seen:
            return
        seen.add(item)
        refs.append(item)

    _add("conversation_message_id", conversation_message_id)
    if message_ref:
        mid = int(message_ref)
        _add("message_id", mid)
        # Глобальный message_id ≠ cmid; как cmid пробуем только «маленькие» id
        # (типичный cmid в диалоге), иначе edit по чужому cmid бессмысленен.
        if mid < 1_000_000:
            _add("conversation_message_id", mid)

    ok = False
    edit_kb = getattr(client, "edit_keyboard_only", None)
    for kind, value in refs:
        kwargs = (
            {"message_id": value}
            if kind == "message_id"
            else {"conversation_message_id": value}
        )
        try:
            if callable(edit_kb):
                if await edit_kb(peer_id, kb, **kwargs):
                    ok = True
                    continue
            # Без непустого message VK отвечает «message is empty or invalid».
            if await client.edit_message(peer_id, "\u200b", keyboard=kb, **kwargs):
                ok = True
        except Exception:
            logger.exception(
                "clear_inline_keyboard failed peer_id=%s %s=%s",
                peer_id,
                kind,
                value,
            )
    return ok

STEP_NAME = "waiting_name"
STEP_PHONE = "waiting_phone"
STEP_GUESTS = "waiting_guests"

# Кнопки самой формы брони. Любая другая cmd при активной сессии
# сбрасывает ввод имени/телефона и отдаёт управление роутеру.
FORM_CMDS: frozenset[str] = frozenset(
    {
        "booking_cancel",
        "booking_get_ticket",
        "booking_name_ok",
        "booking_name_change",
        "booking_phone_use",
        "booking_phone_change",
        "booking_guests",
        "pdn_consent_done",
        "pdn_consent",  # VK_CMD_CONSENT value — дублируем строкой на случай импорта
        "check_booking_start",
    }
)

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


def pdn_consent_keyboard() -> str:
    from bot.pdn_consent import BTN_GIVE_CONSENT, VK_CMD_CONSENT

    kb = VKKeyboardBuilder(inline=True)
    # primary = синяя; зелёная (positive) сливается с эмодзи ✅ после согласия
    kb.button(BTN_GIVE_CONSENT, _payload(VK_CMD_CONSENT), color="primary")
    kb.adjust(1)
    return kb.as_json()


def pdn_consent_accepted_keyboard() -> str:
    from bot.pdn_consent import BTN_CONSENT_ACCEPTED

    kb = VKKeyboardBuilder(inline=True)
    kb.button(BTN_CONSENT_ACCEPTED, _payload("pdn_consent_done"), color="primary")
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
    """Уже есть бронь на этот слот: отмена / сразу список дат Проверки."""
    kb = VKKeyboardBuilder(inline=True)
    kb.button("Отменить бронь", _payload("mb_cancel_confirm", booking_id=booking_id))
    # Как в TG: сразу даты, без шага «по дате / по площадке».
    kb.button("Выбрать другую дату", _payload("check_date_page"), color="positive")
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
        msg_id = await client.send_message(peer_id, text, keyboard=keyboard)
        if msg_id:
            save_confirm_message_id(booking_id, msg_id)
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
        try:
            msg_id = await client.send_message(
                peer_id,
                text,
                keyboard=fallback.as_json(),
            )
            if msg_id:
                save_confirm_message_id(booking_id, msg_id)
        except Exception:
            logger.exception(
                "VK after-booking fallback also failed booking_id=%s peer_id=%s",
                booking_id,
                peer_id,
            )
    return booking_id


async def issue_ticket(
    *,
    client,
    peer_id: int,
    booking_id: int,
    manager_link: str,
    community_link: str = "",
    conversation_message_id: int | None = None,
) -> None:
    booking = get_active_booking_by_id(booking_id)
    if not booking:
        await clear_inline_keyboard(
            client,
            peer_id,
            conversation_message_id=conversation_message_id,
        )
        await client.send_message(peer_id, "Бронь не найдена или уже отменена.")
        return
    status = booking[10]
    already_confirmed = status == "confirmed"
    # Индекс ticket_message_id в BOOKING_SELECT_SQL — 15.
    ticket_message_id = booking[15] if len(booking) > 15 else None
    confirm_message_id = booking[16] if len(booking) > 16 else None

    name = booking[3]
    event_date = booking[5]
    event_time = booking[6]
    event_address = booking[7] or ""
    event_location = booking[8] or ""
    guests = booking[9]

    # Повтор «Получить билет» ок; по прошедшему шоу билет больше не шлём.
    if event_already_passed(event_date, event_time):
        await clear_inline_keyboard(
            client,
            peer_id,
            confirm_message_id,
            conversation_message_id=conversation_message_id,
        )
        await client.send_message(
            peer_id,
            "К сожалению, это мероприятие уже прошло. Посмотри актуальное расписание 😊",
        )
        return

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
    # Места проверяем только при первом подтверждении.
    if not already_confirmed and event:
        confirmed_guests = get_total_guests(event_date, event_time, exclude_id=booking_id)
        if confirmed_guests + guests > event["max_seats"]:
            available = max(0, event["max_seats"] - confirmed_guests)
            await clear_inline_keyboard(
                client,
                peer_id,
                confirm_message_id,
                conversation_message_id=conversation_message_id,
            )
            await client.send_message(
                peer_id,
                "К сожалению, на это мероприятие уже не осталось мест для подтверждения билета.\n\n"
                f"Сейчас свободно: {available}.",
            )
            return

    place = f"{event_location}, {event_address}".strip(", ") if event_location else event_address
    is_raffle = False
    try:
        from bot.db.crud import get_active_raffle_booking, get_booking_format

        fmt = (get_booking_format(booking_id) or "").strip().lower()
        raffle_row = get_active_raffle_booking(vk_id=int(peer_id))
        is_raffle = fmt == "rozygrysh" or bool(
            raffle_row and int(raffle_row[0]) == int(booking_id)
        )
    except Exception:
        pass

    place_on_ticket = format_ticket_place(event_location, event_address)
    ticket_buf = generate_ticket(name or "", event_date, event_time, place_on_ticket, guests)

    vk_manager = (manager_link or "").strip() or MANAGER_LINK
    vk_community = (community_link or "").strip() or CHANNEL_LINK
    head = (
        "<b>Ваш билет ещё раз</b> 👇\n\n"
        if already_confirmed
        else "<b>Отлично!</b>\n\n"
    )

    if is_raffle:
        caption = (
            f"{head}"
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
        from bot.utils.booking_texts import escobar_proverka_ticket_ps

        caption = (
            f"{head}"
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
            f"{escobar_proverka_ticket_ps(location=event_location, address=event_address)}"
        )
        keyboard = manage_ticket_keyboard(booking_id, manager_link)

    # Сначала картинка в чат, потом confirmed — иначе при сбое upload/send
    # в карточке «билет получен», а гость видит только «уже был выдан».
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
    # Билет ушёл — снимаем кнопки с сообщения «Получить билет» (клик + сохранённый id).
    await clear_inline_keyboard(
        client,
        peer_id,
        confirm_message_id,
        conversation_message_id=conversation_message_id,
    )
    save_ticket_message_id(booking_id, msg_id)
    try:
        from bot.db.analytics import EVENT_BOOKING_TICKET_SENT, track_event

        track_event(
            EVENT_BOOKING_TICKET_SENT,
            vk_id=int(peer_id),
            channel="vkontakte",
            booking_id=int(booking_id),
            props={"resent": already_confirmed},
        )
    except Exception:
        pass
    if not already_confirmed:
        update_booking_status(booking_id, "confirmed")
        if is_raffle:
            try:
                from bot.db.crud import set_rozygrysh_used

                set_rozygrysh_used(vk_id=int(peer_id), used=True)
            except Exception:
                logger.exception("set_rozygrysh_used failed booking_id=%s", booking_id)
    elif not ticket_message_id:
        logger.warning(
            "VK ticket resent after confirmed-without-message booking_id=%s peer_id=%s",
            booking_id,
            peer_id,
        )


def saved_phone_for(vk_id: int) -> str | None:
    return normalize_phone(get_last_phone(vk_id=vk_id))
