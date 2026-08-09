"""Общие тексты для подтверждения брони."""

from datetime import datetime
from html import escape

from bot.db.crud import get_same_day_bookings_summary
from bot.utils.ticket import format_date, now_msk

_FORMAT_LABELS = {
    "proverka": "проверка материала",
    "rozygrysh": "розыгрыш",
    "best": "StandUp BEST",
    "hitloto": "Хитлото",
}


def _format_label(format_name: str | None) -> str:
    key = (format_name or "").strip().lower()
    return _FORMAT_LABELS.get(key, format_name or "бронь")


def same_day_booking_warning(
    telegram_id: int | None = None,
    event_date: str = "",
    *,
    exclude_time: str | None = None,
    for_alert: bool = False,
    vk_id: int | None = None,
) -> str:
    """Мягкое предупреждение: на эту дату уже есть другая активная бронь.

    Не блокирует — только текст. exclude_time пропускает то же самое шоу.
    for_alert=True — plain text до 200 символов для Telegram alert.
    telegram_id или vk_id.
    """
    others: list[str] = []
    for time, location, format_name in get_same_day_bookings_summary(
        telegram_id, event_date, exclude_time=exclude_time, vk_id=vk_id
    ):
        time = time or ""
        location = (location or "").strip()
        fmt = _format_label(format_name)
        if time and location:
            label = f"{time} — {location} ({fmt})"
        elif time:
            label = f"{time} ({fmt})"
        else:
            label = fmt
        others.append(label)
    if not others:
        return ""

    date_label = format_date(event_date)
    if for_alert:
        listed = "\n".join(f"• {item}" for item in others)
        text = (
            f"⚠️ На {date_label} уже есть бронь:\n"
            f"{listed}\n"
            "Если планы изменятся — отмените лишнюю."
        )
        return text if len(text) <= 200 else text[:197] + "..."

    listed = "\n".join(f"• {escape(item)}" for item in others)
    return (
        f"⚠️ <b>Обратите внимание:</b> на {escape(date_label)} у вас уже есть бронь:\n"
        f"{listed}\n\n"
        "Если планы изменятся, отмените лишнюю бронь."
    )


def reminder_details_cut(*, event_time: str, location_line: str, guests: int) -> str:
    """Длинный блок «Напоминаем» под раскрывающийся кат (expandable blockquote)."""
    location = escape((location_line or "").strip())
    time = escape(event_time or "")
    return (
        "<blockquote expandable>"
        "📋 <b>Напоминаем:</b>\n"
        f"1. Сбор гостей начинается за полчаса до начала шоу, старт в {time}\n"
        "2. Рассадка осуществляется администратором рассадки на ближайшие к сцене свободные места. "
        "Возможна подсадка за один стол других гостей для небольших компаний.\n"
        "3. Обратите внимание, что при посещении шоу заказ минимум одной позиции по меню является обязательным.\n"
        f"4. {location}\n"
        f"5. Количество гостей — {guests} чел.\n"
        "6. Если поменяются планы, пожалуйста, ОБЯЗАТЕЛЬНО ПРЕДУПРЕДИТЕ 😊"
        "</blockquote>"
    )


def ticket_window_open(event_date: str) -> bool:
    """True если уже можно получить билет (за сутки и в день шоу)."""
    try:
        days_until = (datetime.strptime(event_date, "%d.%m.%Y").date() - now_msk().date()).days
    except Exception:
        return False
    return days_until <= 1


def proverka_reminder_24h_text(
    *,
    date_str: str,
    event_time: str,
    address_line: str,
    guests: int,
    expandable: bool = True,
) -> str:
    """Текст напоминания за сутки для проверки материала (TG/VK)."""
    time = escape(event_time or "")
    address = escape((address_line or "").strip())
    if address and not address.lower().startswith("адрес"):
        address_display = f"📍 Адрес {address}"
        if not address_display.rstrip().endswith("."):
            address_display += "."
    else:
        address_display = f"📍 {address}" if address else "📍 Адрес"
    body = (
        f"Привет! 😊 Пишу подтвердить бронь на завтрашнюю проверку материала "
        f"({escape(date_str)})! 🎤\n\n"
        "<u>Чтобы подтвердить бронь, нажми на кнопку «Получить билет»</u>\n"
        "❗ <b>Внимание, если Вы не успеете подтвердить бронь, она будет аннулирована.</b>\n\n"
        "Напоминаем, что :\n"
        f"1. Сбор гостей начинается за полчаса до начала шоу, старт в {time}\n"
        "2. Рассадка осуществляется по мере прихода, чтобы занять лучшие места, "
        "приходите вовремя ☝️ Возможна подсадка за один стол других гостей "
        "для небольших компаний.\n"
        "3. Обратите внимание, что при посещении шоу заказ минимум одной позиции "
        "по меню является обязательным.\n"
        f"4. <u>{address_display}</u>\n"
        f"5. Количество гостей - {int(guests)} чел.\n"
        "6. Если поменяются планы, пожалуйста, ОБЯЗАТЕЛЬНО ПРЕДУПРЕДИТЕ 😊"
    )
    if expandable:
        return f"<blockquote expandable>{body}</blockquote>"
    return body


def raffle_reminder_24h_text(
    *,
    event_time: str,
    location_line: str,
    guests: int,
    expandable: bool = True,
) -> str:
    """Текст напоминания за сутки для розыгрыша (как в TG)."""
    details = reminder_details_cut(
        event_time=event_time,
        location_line=location_line,
        guests=guests or 1,
    )
    if not expandable:
        details = (
            details.replace("<blockquote expandable>", "")
            .replace("</blockquote>", "")
        )
    return (
        "Привет! 😊 Пишу подтвердить бронь на завтрашнее ШОУ! 🎤\n\n"
        "<b>Чтобы подтвердить бронь, нажми на кнопку «Получить билет»</b>\n"
        "❗️ <b>Внимание, если Вы не успеете подтвердить бронь, она будет аннулирована.</b>\n\n"
        f"{details}"
    )
