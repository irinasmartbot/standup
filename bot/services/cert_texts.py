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
START_WORDS = frozenset({"сертификат", "сертификаты"})
START_TEXT = (
    "Привет 😊 Сейчас оформим бронь по бумажному сертификату.\n\n"
    "Как это работает:\n"
    "1. На бланке есть QR — открой страницу, она выдаст номер, "
    "например <b>MSS-000001</b>.\n"
    "2. Напиши этот номер сюда одним сообщением.\n"
    "3. Потом пришли фото, где номер хорошо видно — мы проверим.\n"
    "4. После этого оформим бронь по сертификату на два лица: Хитлото, BEST "
    "или Елка для взрослых.\n\n"
    "Напиши номер с сертификата ✍️"
)
NUMBER_ASK_TEXT = (
    "Нужен номер с бланка, который выдала страница сертификата ✍️\n"
    "Одним сообщением, буквы, цифры и дефис. Например: <b>MSS-000001</b>"
)
NUMBER_NOT_FOUND_TEXT = (
    "Этот номер в реестре не нашёлся 🙈\n\n"
    "Нужен номер, который выдала страница после QR на бланке. "
    "Обычно он выглядит как <b>MSS-000001</b>.\n\n"
    "Открой страницу по QR, скопируй номер и пришли его сюда одним сообщением ✍️"
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
    "Как только номер подтвердят — откроется бронь по сертификату на два лица."
)
REJECT_TEXT = (
    "Фото пока не приняли 🙈\n"
    "Напиши номер на сертификате так, чтобы его было видно, и пришли новое фото."
)
APPROVED_TEXT = (
    "Сертификат принят 🎉\n\n"
    "Как тебя записать? Напиши имя ✍️"
)
NAME_ASK_TEXT = "Как тебя записать в бронь? Напиши имя ✍️"
PHONE_ASK_TEXT = (
    "И телефон, пожалуйста 📱\n"
    "Можно кнопкой ниже или сообщением, например <code>+79001234567</code>"
)
PHONE_CONFIRM_TEXT = (
    "Ваш номер телефона: <b>{phone}</b>\n"
    "Использовать его?"
)
PHONE_OK_BUTTON = "✅ Да, использовать"
PHONE_OTHER_BUTTON = "✏️ Другой номер"
FORMAT_ASK_TEXT = "Куда идём по сертификату? 😊"
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
        "Бронь по сертификату на два лица.\n\n"
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
        "бронь по сертификату на два лица",
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


def booked_thanks_text(event: dict, *, fmt: str, days_until: int) -> str:
    """Как после обычной брони: спасибо, за сутки напомним. Не подтверждение."""
    label = escape(format_label(fmt or event.get("format") or ""))
    date_str = escape(_human_date(event.get("date") or ""))
    weekday = escape((event.get("weekday") or "").strip())
    time_str = escape((event.get("time") or "").strip())
    address = escape((event.get("address") or "").strip())
    weekday_part = f" ({weekday})" if weekday else ""
    if days_until <= 1:
        return (
            "Отлично!\n\n"
            "❗ <b>Важная информация</b> — для того чтобы мы окончательно закрепили за Вами место "
            "на дату и время:\n"
            f"<b>Дата:</b> {date_str}{weekday_part}\n"
            f"<b>Время:</b> {time_str}\n"
            f"<b>Формат:</b> сертификат · {label}\n"
            f"<b>Количество гостей:</b> 2 чел.\n\n"
            "<b>ОБЯЗАТЕЛЬНО подтвердите бронь, нажав на кнопку «Подтвердить бронь»</b>\n\n"
            "❗ Внимание, если Вы не успеете подтвердить бронь, она будет аннулирована."
        )
    return (
        "Отлично! Мы внесли Вас в списки гостей:\n\n"
        f"<b>Дата:</b> {date_str}{weekday_part}\n"
        f"<b>Время:</b> {time_str}\n"
        f"<b>Локация:</b> {address}\n"
        f"<b>Формат:</b> сертификат · {label}\n"
        f"<b>Количество гостей:</b> 2 чел.\n\n"
        "<b>❗ Внимание, за сутки до мероприятия Вам придёт сообщение-напоминание с подробностями "
        "и кнопкой «Подтвердить бронь». Обязательно нажмите кнопку, чтобы подтвердить бронь. "
        "Если Вы не успеете подтвердить бронь, она будет аннулирована.</b>\n\n"
        "Если поменяются планы, обязательно предупредите 😊"
    )


def reminder_24h_text(
    *,
    name: str,
    event_time: str,
    location_line: str,
    expandable: bool = True,
) -> str:
    loc = escape((location_line or "").strip())
    time = escape(event_time or "")
    who = escape(name or "")
    details = (
        "📋 <b>Напоминаем:</b>\n"
        f"1. Сбор гостей начинается за полчаса до начала шоу, старт в {time}\n"
        "2. Рассадка осуществляется администратором рассадки на ближайшие к сцене свободные места. "
        "Возможна подсадка за один стол других гостей для небольших компаний.\n"
        "3. Обратите внимание, что при посещении шоу заказ минимум одной позиции по меню является обязательным.\n"
        f"4. {loc}\n"
        "5. Количество гостей — 2 чел.\n"
        "6. Возьми бумажный сертификат с собой и отдай его администратору рассадки. Без него на шоу не пустят.\n"
        "7. Если поменяются планы, пожалуйста, ОБЯЗАТЕЛЬНО ПРЕДУПРЕДИТЕ 😊"
    )
    if expandable:
        details = f"<blockquote expandable>{details}</blockquote>"
    return (
        f"{who}, привет! 😊 Пишу подтвердить бронь по сертификату на завтрашнее шоу! 🎤\n\n"
        "<b>Чтобы подтвердить бронь, нажми на кнопку «Подтвердить бронь»</b>\n"
        "❗️ <b>Внимание, если Вы не успеете подтвердить бронь, она будет аннулирована, "
        "а номер сертификата останется свободным.</b>\n\n"
        f"{details}"
    )


def reminder_day_text(*, name: str, date_str: str, event_time: str) -> str:
    return (
        f"{escape(name or '')}, мне необходимо подтвердить, либо отменить Вашу бронь "
        f"по сертификату на {escape(date_str)} в {escape(event_time or '')} 😊\n\n"
        "<u>Чтобы подтвердить бронь, нажми на «Подтвердить бронь»</u> 👇"
    )


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
