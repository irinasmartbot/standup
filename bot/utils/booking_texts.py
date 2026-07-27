"""Общие тексты для подтверждения брони."""

from html import escape

from bot.db.crud import get_same_day_bookings_summary
from bot.utils.ticket import format_date

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
