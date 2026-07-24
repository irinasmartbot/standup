"""Общие тексты для подтверждения брони."""

from html import escape

from bot.db.crud import get_active_bookings_by_user
from bot.utils.ticket import format_date


def same_day_booking_warning(
    telegram_id: int,
    event_date: str,
    *,
    exclude_time: str | None = None,
) -> str:
    """Мягкое предупреждение: на эту дату уже есть другая активная бронь.

    Не блокирует — только текст. exclude_time пропускает то же самое шоу.
    """
    others: list[str] = []
    for booking in get_active_bookings_by_user(telegram_id):
        if (booking[5] or "") != event_date:
            continue
        if exclude_time and (booking[6] or "") == exclude_time:
            continue
        time = booking[6] or ""
        location = (booking[8] or "").strip()
        label = f"{time}" + (f" — {location}" if location else "")
        others.append(label)
    if not others:
        return ""

    date_label = format_date(event_date)
    listed = "\n".join(f"• {escape(item)}" for item in others)
    return (
        f"⚠️ <b>Обратите внимание:</b> на {escape(date_label)} у вас уже есть бронь:\n"
        f"{listed}\n\n"
        "Шоу проходят рядом — теоретически можно успеть на оба. "
        "Жёстких ограничений нет. Если планы изменятся, отмените лишнюю бронь."
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
