"""Тексты гостю по обычному сертификату."""

from datetime import datetime
from html import escape

_MONTHS = {
    "January": "января",
    "February": "февраля",
    "March": "марта",
    "April": "апреля",
    "May": "мая",
    "June": "июня",
    "July": "июля",
    "August": "августа",
    "September": "сентября",
    "October": "октября",
    "November": "ноября",
    "December": "декабря",
}


def _human_date(date_str: str) -> str:
    try:
        day = datetime.strptime(date_str, "%d.%m.%Y")
        return day.strftime("%d ") + _MONTHS[day.strftime("%B")]
    except Exception:
        return date_str or ""

FORMAT_LABELS = {
    "hitloto": "Хитлото",
    "best": "BEST",
    "elka": "Елка для взрослых",
}
FORMAT_BUTTONS = (
    ("🎱 Хитлото", "hitloto"),
    ("🎤 BEST", "best"),
    ("🎄 Елка для взрослых", "elka"),
)

BRING_TEXT = (
    "Возьми бумажный сертификат с собой и отдай его администратору рассадки 🎫 "
    "Без него на шоу не пустят."
)
WARN_TEXT = (
    "После подтверждения дату уже не перенести. "
    "Если подтвердить бронь и не прийти — сертификат сгорит 🔥"
)
CAP_TEXT = "на указанную дату бесплатных мест по сертификату не осталось"
UNAVAILABLE_TEXT = "Сейчас сертификат оформить не получается 😔 Напиши менеджеру — разберёмся."
PREVIEW_TEXT = "Это номер из предпросмотра, он не действует."
JACKPOT_TEXT = (
    "Этот сертификат — джекпот 🎰 Его пока нельзя оформить в боте. Напиши менеджеру."
)
NUMBER_ASK_TEXT = (
    "Напиши номер с сертификата одним сообщением ✍️\n"
    "Буквы, цифры и дефис. Например: <b>MSS-000001</b>"
)
PHOTO_ASK_TEMPLATE = (
    "Номер сертификата: <b>{code}</b>\n\n"
    "Впиши его на лицевую сторону, если ещё не вписан, "
    "и пришли одно фото, где номер хорошо читается 📸"
)
MISMATCH_TEXT = (
    "Номер не совпадает с тем, который выдала страница. "
    "Проверь и отправь ещё раз 🙏"
)
PENDING_TEXT = (
    "Фото ушло на проверку ⏳\n"
    "Как только номер подтвердят — откроется бронь на двоих."
)
REJECT_TEXT = (
    "Фото пока не приняли 🙈\n"
    "Напиши номер на сертификате так, чтобы его было видно, и пришли новое фото."
)
APPROVED_TEXT = (
    "Сертификат принят 🎉\n\n"
    "Бронь на <b>двоих</b>, одну дату и один формат: "
    "Хитлото, BEST или Елка для взрослых.\n\n"
    "Как тебя записать? Напиши имя ✍️"
)
NAME_ASK_TEXT = "Как тебя записать в бронь? Напиши имя ✍️"
PHONE_ASK_TEXT = (
    "И телефон, пожалуйста 📱\n"
    "Можно кнопкой ниже или сообщением, например <code>+79001234567</code>"
)
FORMAT_ASK_TEXT = (
    "Куда идём по сертификату? 😊\n"
    "Места сразу на двоих, количество не меняется."
)
DATE_ASK_TEXT = "Выбирай дату 👇"
EMPTY_DATES_TEXT = "На этот формат пока нет дат 😔 Выбери другой."
ALREADY_BOOKED_TEXT = "На это шоу у тебя уже есть бронь 🙂"
RESERVE_PENDING_TEXT = (
    "Бронь ещё записывается ⏳ Не создавай вторую — нажми ту же дату ещё раз через минуту."
)
CONFIRMED_TEXT = "Бронь подтверждена, сертификат списан ✅\n\n" + BRING_TEXT
CANCELLED_TEXT = (
    "Ок, бронь сняла 🙂 Этот номер сертификата можно использовать на другую дату."
)
TRANSFER_TEXT = (
    "Давай выберем новую дату 📅 Прежняя бронь сохранится, пока новая не запишется."
)
CONFIRM_BUTTON = "✅ Подтвердить бронь"
CANCEL_BUTTON = "❌ Отменить бронь"
MOVE_BUTTON = "📅 Перенести дату"
BOOK_BUTTON = "🎟 Забронировать"
BACK_FORMAT_BUTTON = "◀️ Другой формат"
BACK_DATE_BUTTON = "◀️ Другая дата"
NO_TICKET_TEXT = "По сертификату билет картинкой не выдаём. На входе нужен сам сертификат 🎫"


def format_label(fmt: str) -> str:
    return FORMAT_LABELS.get((fmt or "").strip().lower(), "шоу")


def date_ask_text(fmt: str) -> str:
    label = format_label(fmt)
    return (
        f"Привет 😊 По сертификату забронируем места на <b>{escape(label)}</b> "
        "от Moscow StandUp Show 🎤\n"
        "Вас двое, количество не меняется.\n\n"
        "Выбирай дату 👇"
    )


def event_card_caption(event: dict, *, fmt: str) -> str:
    label = format_label(fmt or event.get("format") or "")
    date_str = _human_date(event.get("date") or "")
    weekday = escape((event.get("weekday") or "").strip())
    time_str = escape((event.get("time") or "").strip())
    address = escape((event.get("address") or "").strip())
    location = escape((event.get("location") or "").strip())
    description = escape((event.get("description") or "").strip())
    where = address or location
    parts = [
        f"🎫 <b>Сертификат · {escape(label)}</b>",
        "места на двоих",
        "",
        f"<b>{escape(date_str)}</b>",
    ]
    if weekday:
        parts.append(weekday)
    parts.append("")
    if time_str:
        parts.append(f"<b>{time_str}</b>")
    if where:
        parts.append(where)
    if description and description not in {where, address}:
        parts.append(description)
    text = "\n".join(parts)
    if len(text) > 1024:
        text = text[:1021].rstrip() + "..."
    return text


def reserved_caption(event: dict, *, fmt: str) -> str:
    label = format_label(fmt or event.get("format") or "")
    date_str = _human_date(event.get("date") or "")
    time_str = (event.get("time") or "").strip()
    location = (event.get("location") or "").strip()
    address = (event.get("address") or "").strip()
    where = ", ".join(part for part in (location, address) if part)
    lines = [
        f"✅ Записала бронь по сертификату на <b>{escape(label)}</b>",
        f"{escape(date_str)} в {escape(time_str)} · двое гостей",
    ]
    if where:
        lines.append(escape(where))
    lines.extend(["", WARN_TEXT, "", BRING_TEXT])
    return "\n".join(lines)


def confirmed_caption(booking: dict) -> str:
    label = format_label(booking.get("event_format") or "")
    date_str = _human_date(booking.get("event_date") or "")
    time_str = (booking.get("event_time") or "").strip()
    location = (booking.get("location") or "").strip()
    return (
        f"✅ Бронь по сертификату подтверждена — <b>{escape(label)}</b>\n"
        f"{escape(date_str)} в {escape(time_str)}"
        + (f", {escape(location)}" if location else "")
        + "\nВас двое.\n\n"
        + BRING_TEXT
    )
