import logging
import os
import random
from datetime import datetime
from html import escape

from aiogram import Router
from aiogram.types import CallbackQuery, FSInputFile, InputMediaPhoto, InputRichMessage
from aiogram.utils.keyboard import InlineKeyboardBuilder
from bot.config import MANAGER_LINK, CHANNEL_LINK, TICKET_TEMPLATE
from bot.services.sheets import load_events
from bot.utils.ticket import MONTHS, format_date
from bot.utils.nav_messages import remember_booking_nav, forget_booking_nav, delete_booking_nav

router = Router()
logger = logging.getLogger(__name__)

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
PHOTOS_DIR = os.path.join(_PROJECT_ROOT, "фото")
WELCOME_MARKER = "Здесь можно забронировать места на бесплатные шоу или купить билеты на StandUp BEST и Хитлото."
VENUE_PHOTO_FILES = {"temple_bar.jpg", "escobar.jpg", "nebar.jpg"}
_VENUE_ALBUM_MESSAGE_IDS = {}
BEST_DATES_PAGE_SIZE = 10

# Обычный HTML (fallback, если Rich Messages недоступны)
FORMATS_TEXT = """🎭 <b>Наши форматы шоу:</b>

⭐ <b>StandUp BEST</b>
Только лучший, уже проверенный стэндап материал от троих комиков, именитых участников многочисленных телевизионных проектов. Вы не услышите ни одной несмешной шутки, только BEST!!
🎟 Билеты — от <b>500 ₽</b>

🎵 <b>Хитлото</b>
Музыкальное лото в компании стендап-комика! Веселись, пой, пей, зачеркивай прозвучавшие песни в своём бланке, и выигрывай призы 🔥
🎟 Билеты — от <b>990 ₽</b>

🎤 <b>StandUp Проверка материала</b>
5–7 опытных комиков, участников известных проектов ТНТ и YouTube, рассказывают по 10–15 минут свежих, но не проверенных шуток. Вы услышите настоящий эксклюзив и поможете комикам понять, что смешно, а что стоит убрать из материала 🙈
🆓 Вход <b>бесплатный</b>."""

BUY_TICKET_TEXT = (
    "Здесь можно посмотреть афишу по платным форматам и купить билет.\n\n"
    "<b>Выбирай формат шоу</b>\n\n"
    "<b>StandUp BEST</b>\n"
    "<i>Только лучший, уже проверенный стэндап материал от троих комиков, "
    "именитых участников многочисленных телевизионных проектов. "
    "Вы не услышите ни одной несмешной шутки, только BEST!!</i>\n"
    "Билеты — от <b>500 ₽</b>\n\n"
    "<b>Хитлото</b>\n"
    "<i>Музыкальное лото в компании стендап-комика! Веселись, пой, пей, "
    "зачеркивай прозвучавшие песни в своём бланке, и выигрывай призы.</i>\n"
    "Билеты — от <b>990 ₽</b>."
)

# Rich Messages HTML: крупные заголовки как в редакторе Telegram
FORMATS_RICH_HTML = """
<h2>🎭 Наши форматы шоу</h2>
<h3>⭐ StandUp BEST</h3>
<p>Только лучший, уже проверенный стэндап материал от троих комиков, именитых участников многочисленных телевизионных проектов. Вы не услышите ни одной несмешной шутки, только BEST!!</p>
<p>🎟 Билеты — от <b>500 ₽</b></p>
<h3>🎵 Хитлото</h3>
<p>Музыкальное лото в компании стендап-комика! Веселись, пой, пей, зачеркивай прозвучавшие песни в своём бланке, и выигрывай призы 🔥</p>
<p>🎟 Билеты — от <b>990 ₽</b></p>
<h3>🎤 StandUp Проверка материала</h3>
<p>5–7 опытных комиков, участников известных проектов ТНТ и YouTube, рассказывают по 10–15 минут свежих, но не проверенных шуток. Вы услышите настоящий эксклюзив и поможете комикам понять, что смешно, а что стоит убрать из материала 🙈</p>
<p>🆓 Вход <b>бесплатный</b></p>
"""

BUY_TICKET_RICH_HTML = """
<p>Здесь можно посмотреть афишу по платным форматам и купить билет.</p>
<h2>Выбирай формат шоу</h2>
<h3>StandUp BEST</h3>
<p><i>Только лучший, уже проверенный стэндап материал от троих комиков, именитых участников многочисленных телевизионных проектов. Вы не услышите ни одной несмешной шутки, только BEST!!</i></p>
<p>Билеты — от <b>500 ₽</b></p>
<h3>Хитлото</h3>
<p><i>Музыкальное лото в компании стендап-комика! Веселись, пой, пей, зачеркивай прозвучавшие песни в своём бланке, и выигрывай призы.</i></p>
<p>Билеты — от <b>990 ₽</b></p>
"""

QUICK_BOOKING_GREETING_RICH = (
    "<p>Привет! Я — бот <b>Moscow StandUp Show</b> для бронирования мест на мероприятия.</p>"
)
QUICK_BOOKING_GREETING_TEXT = (
    "Привет! Я — бот <b>Moscow StandUp Show</b> для бронирования мест на мероприятия.\n\n"
)


async def _send_rich_or_html(message, *, rich_html: str, fallback_html: str, reply_markup=None):
    """Сначала Rich Messages (крупные заголовки), при ошибке — обычный HTML."""
    try:
        return await message.bot.send_rich_message(
            chat_id=message.chat.id,
            rich_message=InputRichMessage(html=rich_html),
            reply_markup=reply_markup,
        )
    except Exception:
        logger.exception("send_rich_message failed; falling back to HTML")
        return await message.answer(
            fallback_html,
            reply_markup=reply_markup,
            parse_mode="HTML",
        )

VENUES_OUTRO_RICH_HTML = """
<h2>Наши площадки</h2>
<p>Мероприятия проходят в заведениях, где каждый найдёт что-то на свой вкус: для любителей вкусно покушать — рестораны с изысканной кухней разных народов мира, для поклонников шумных вечеринок — бары, для любителей попеть — заведения с караоке. Везде можно остаться после шоу.</p>
"""

VENUES_OUTRO_TEXT = (
    "<b>Наши площадки</b>\n\n"
    "Мероприятия проходят в заведениях, где каждый найдёт что-то на свой вкус: "
    "для любителей вкусно покушать — рестораны с изысканной кухней разных народов мира, "
    "для поклонников шумных вечеринок — бары, для любителей попеть — заведения с караоке. "
    "Везде можно остаться после шоу."
)

VENUE_CARDS = (
    {
        "file": "temple_bar.jpg",
        "rich_html": """
<h2>🍽 Temple Bar</h2>
<p><i>Английская респектабельность · ирландское жизнелюбие · русское гостеприимство</i></p>
<blockquote>Ресторан, где каждый гость чувствует демократичную атмосферу: великолепные стейки, большой ассортимент коктейлей и отменные блюда из мяса и овощей на мангале.</blockquote>
<p>🥩 Стейки · 🍹 Коктейли · 🔥 Мангал</p>
""",
        "fallback_html": (
            "🍽 <b>Temple Bar</b>\n"
            "<i>Английская респектабельность · ирландское жизнелюбие · русское гостеприимство</i>\n\n"
            "<blockquote>Ресторан, где каждый гость чувствует демократичную атмосферу: "
            "великолепные стейки, большой ассортимент коктейлей и отменные блюда "
            "из мяса и овощей на мангале.</blockquote>\n\n"
            "🥩 Стейки · 🍹 Коктейли · 🔥 Мангал"
        ),
    },
    {
        "file": "escobar.jpg",
        "rich_html": """
<h2>🍸 Escobar</h2>
<p><i>Брутальный бар в эстетике Тарантино</i></p>
<blockquote>Бар с неординарной кухней в комплексе исторических зданий 18–19 веков. Брутальный дизайн с лёгким оттенком латиноамериканской расслабленности.</blockquote>
<p>🎬 Киноаскетика · 🏛 Исторический центр · 🌶 Необычная кухня</p>
""",
        "fallback_html": (
            "🍸 <b>Escobar</b>\n"
            "<i>Брутальный бар в эстетике Тарантино</i>\n\n"
            "<blockquote>Бар с неординарной кухней в комплексе исторических зданий "
            "18–19 веков. Брутальный дизайн с лёгким оттенком латиноамериканской "
            "расслабленности.</blockquote>\n\n"
            "🎬 Киноаскетика · 🏛 Исторический центр · 🌶 Необычная кухня"
        ),
    },
    {
        "file": "nebar.jpg",
        "rich_html": """
<h2>🔊 Небар</h2>
<p><i>Один из самых популярных и громких баров столицы</i></p>
<blockquote>Уникальный стиль и авторская коктейльная карта для тех, кто любит эксперименты: 13 сезонных коктейлей на любой вкус, названных в честь известных городов мира.</blockquote>
<p>🍸 Авторские коктейли · 🌃 Атмосфера · 🎉 Громко и ярко</p>
""",
        "fallback_html": (
            "🔊 <b>Небар</b>\n"
            "<i>Один из самых популярных и громких баров столицы</i>\n\n"
            "<blockquote>Уникальный стиль и авторская коктейльная карта для тех, "
            "кто любит эксперименты: 13 сезонных коктейлей на любой вкус, "
            "названных в честь известных городов мира.</blockquote>\n\n"
            "🍸 Авторские коктейли · 🌃 Атмосфера · 🎉 Громко и ярко"
        ),
    },
)

RULES_TEXT = """📋 <b>Правила посещения шоу:</b>

1️⃣ <b>Возраст</b>
На наших мероприятиях действует возрастное ограничение 18+

2️⃣ <b>Время</b>
Сбор гостей начинается за полчаса до времени начала мероприятия.

3️⃣ <b>Обязательный заказ</b>
Все шоу проходят в заведениях в центре Москвы, посещение шоу предполагает обязательный заказ минимум одной позиции по меню заведения.

4️⃣ <b>Рассадка</b>
Рассадка осуществляется администратором на площадке.
Для формата Проверка материала: рассадка осуществляется по мере прихода, начиная от сцены.
Для формата StandUp Best: рассадка осуществляется в соответствии с местом в билете, при опоздании более чем на 10 минут посетитель теряет право на место.

5️⃣ <b>Тишина</b>
Во время шоу запрещено громко разговаривать, выкрикивать с места, говорить по телефону. При многократном нарушении администратор может попросить Вас удалиться из зала без возможности возврата средств."""


def _manager_username():
    return "@" + MANAGER_LINK.rstrip("/").split("/")[-1]


def _random_format_photo():
    ticket_name = os.path.basename(TICKET_TEMPLATE)
    try:
        files = [
            f for f in os.listdir(PHOTOS_DIR)
            if f.lower().endswith((".jpg", ".jpeg", ".png", ".webp"))
            and f != ticket_name
            and f.lower() not in VENUE_PHOTO_FILES
            and f.lower() != "ticket_template.jpg"
            and not f.lower().startswith("rozygrysh_otzyv")
            and not f.lower().startswith("hitloto")
        ]
    except FileNotFoundError:
        files = []
    if files:
        return FSInputFile(os.path.join(PHOTOS_DIR, random.choice(files)))
    return None


def _hitloto_photo():
    try:
        files = [
            f for f in os.listdir(PHOTOS_DIR)
            if f.lower().startswith("hitloto")
            and f.lower().endswith((".jpg", ".jpeg", ".png", ".webp"))
        ]
    except FileNotFoundError:
        files = []
    if files:
        return FSInputFile(os.path.join(PHOTOS_DIR, sorted(files)[0]))
    return _random_format_photo()


async def _answer_with_format_photo(message, text: str, reply_markup=None, parse_mode=None, track_nav=False):
    sent = None
    photo = _random_format_photo()
    if photo:
        try:
            sent = await message.answer_photo(photo=photo, caption=text, reply_markup=reply_markup, parse_mode=parse_mode)
        except Exception:
            sent = None
    if sent is None:
        sent = await message.answer(text, reply_markup=reply_markup, parse_mode=parse_mode)
    if track_nav and sent:
        remember_booking_nav(message.chat.id, sent.message_id)
    return sent


async def _answer_with_hitloto_photo(message, text: str, reply_markup=None, parse_mode=None, track_nav=False):
    sent = None
    photo = _hitloto_photo()
    if photo:
        try:
            sent = await message.answer_photo(photo=photo, caption=text, reply_markup=reply_markup, parse_mode=parse_mode)
        except Exception:
            sent = None
    if sent is None:
        sent = await message.answer(text, reply_markup=reply_markup, parse_mode=parse_mode)
    if track_nav and sent:
        remember_booking_nav(message.chat.id, sent.message_id)
    return sent


async def _best_dates_kb(page: int = 0):
    events = await load_events("best")
    dates = sorted(set(e["date"] for e in events), key=lambda d: datetime.strptime(d, "%d.%m.%Y"))
    page = max(page, 0)
    start = page * BEST_DATES_PAGE_SIZE
    end = start + BEST_DATES_PAGE_SIZE
    shown_dates = dates[start:end]
    kb = InlineKeyboardBuilder()
    for date in shown_dates:
        try:
            d = datetime.strptime(date, "%d.%m.%Y")
            label = d.strftime("%d ") + MONTHS[d.strftime("%B")]
        except Exception:
            label = date
        kb.button(text=label, callback_data=f"best_date_{date}")
    nav_count = 0
    if page > 0:
        kb.button(text="⬅️ Назад", callback_data=f"best_dates_page_{page - 1}")
        nav_count += 1
    if end < len(dates):
        kb.button(text="Показать ещё ➡️", callback_data=f"best_dates_page_{page + 1}")
        nav_count += 1
    kb.button(text="📍 Выбор по площадкам", callback_data="best_venues")
    kb.button(text="◀️ Назад в меню", callback_data="main_menu")
    if shown_dates:
        widths = [2] * (len(shown_dates) // 2)
        if len(shown_dates) % 2:
            widths.append(1)
        if nav_count:
            widths.append(nav_count)
        widths.extend([1, 1])
        kb.adjust(*widths)
    else:
        kb.adjust(1, 1)
    return kb.as_markup()


async def _best_venues_kb():
    events = await load_events("best")
    venues = sorted(set(e["location"] for e in events))
    kb = InlineKeyboardBuilder()
    for venue in venues:
        kb.button(text=venue, callback_data=f"best_venue_{venue}")
    kb.button(text="📅 Выбор по дате", callback_data="best_dates")
    kb.button(text="◀️ Назад в меню", callback_data="main_menu")
    kb.adjust(*([1] * len(venues)), 1, 1)
    return kb.as_markup()


def _event_sort_key(event):
    try:
        return datetime.strptime(f"{event['date']} {event['time']}", "%d.%m.%Y %H:%M")
    except Exception:
        return datetime.max


def _best_event_by_id(events, event_id):
    return next((e for e in events if str(e["id"]) == event_id), None)


def _parse_best_event_callback(data):
    payload = data.replace("best_event_", "", 1)
    if "_date_" in payload:
        event_id, date = payload.split("_date_", 1)
        return event_id, f"best_date_{date}"
    if "_venue_" in payload:
        event_id, venue = payload.split("_venue_", 1)
        return event_id, f"best_venue_{venue}"
    return payload, "best_dates"


def _host_lines(host: str) -> list[str]:
    """Разбивает поле host из БД на строки по людям."""
    text = (host or "").replace("\r\n", "\n").strip()
    if not text:
        return []
    lines = [line.strip(" -–—\t") for line in text.split("\n") if line.strip()]
    if len(lines) == 1 and " - " in lines[0]:
        # Иногда несколько человек в одной строке через перевод строки уже есть;
        # если одна длинная строка — оставляем как есть.
        pass
    return [line for line in lines if line]


def _format_host_quote(host: str, *, title: str) -> str:
    """Красивая цитата с составом для карточки события."""
    lines = _host_lines(host)
    if not lines:
        return ""
    body = "\n".join(f"🎤 {escape(line)}" for line in lines)
    # expandable — если состав длинный, цитату можно свернуть
    tag = "blockquote expandable" if len(body) > 280 else "blockquote"
    return f"<b>{escape(title)}</b>\n<{tag}>{body}</{tag.split()[0]}>"


def _best_event_text(event, *, host_title: str = "Кто выступает"):
    parts = [
        f"<b>{format_date(event['date'])}</b>",
        escape((event.get("weekday") or "").strip()),
        "",
        f"<b>{escape((event.get('time') or '').strip())}</b>",
        escape((event.get("address") or "").strip()),
        escape((event.get("description") or "").strip()),
    ]
    host_quote = _format_host_quote(event.get("host") or "", title=host_title)
    if host_quote:
        # без пустых строк между описанием и «Кто выступает» / «Ведущий»
        parts = [p for p in parts if p is not None]
        while parts and parts[-1] == "":
            parts.pop()
        parts.append(host_quote)
    text = "\n".join(parts)
    # Лимит подписи к фото в Telegram — 1024 символа
    if len(text) > 1024:
        text = text[:1021].rstrip() + "..."
    return text


async def _send_best_event_card(message, event, back_callback="best_dates"):
    kb = InlineKeyboardBuilder()
    payment_url = event.get("payment_url") or ""
    if payment_url:
        kb.button(text="🎟 Купить билет", url=payment_url)
    else:
        kb.button(text="💬 Задать вопрос менеджеру", url=MANAGER_LINK)
    kb.button(text="◀️ Назад", callback_data=back_callback)
    kb.adjust(1)
    text = _best_event_text(event)
    image = event.get("image") or ""
    if image:
        try:
            await message.answer_photo(photo=image, caption=text, reply_markup=kb.as_markup(), parse_mode="HTML")
            return
        except Exception:
            try:
                await message.answer_photo(photo=image)
            except Exception:
                pass
    await message.answer(text, reply_markup=kb.as_markup(), parse_mode="HTML")


def _best_location_carousel_kb(events, index: int):
    event = events[index]
    kb = InlineKeyboardBuilder()
    payment_url = event.get("payment_url") or ""
    if payment_url:
        kb.button(text="🎟 Купить билет", url=payment_url)
    else:
        kb.button(text="💬 Задать вопрос менеджеру", url=MANAGER_LINK)

    nav_buttons = 0
    if index > 0:
        kb.button(text="‹", callback_data=f"best_loc_carousel_{event['id']}_prev")
        nav_buttons += 1
    kb.button(text=f"{index + 1}/{len(events)}", callback_data="best_carousel_position")
    nav_buttons += 1
    if index < len(events) - 1:
        kb.button(text="›", callback_data=f"best_loc_carousel_{event['id']}_next")
        nav_buttons += 1

    kb.button(text="◀️ Назад", callback_data="best_venues")
    kb.adjust(1, nav_buttons, 1)
    return kb.as_markup()


async def _send_best_location_carousel(message, events, index: int = 0):
    event = events[index]
    text = _best_event_text(event)
    image = event.get("image") or ""
    markup = _best_location_carousel_kb(events, index)
    if image:
        try:
            await message.answer_photo(photo=image, caption=text, reply_markup=markup, parse_mode="HTML")
            return
        except Exception:
            try:
                await message.answer_photo(photo=image)
            except Exception:
                pass
    await message.answer(text, reply_markup=markup, parse_mode="HTML")


async def _edit_best_location_carousel(call: CallbackQuery, events, index: int):
    event = events[index]
    text = _best_event_text(event)
    image = event.get("image") or ""
    markup = _best_location_carousel_kb(events, index)

    if image:
        try:
            await call.message.edit_media(
                media=InputMediaPhoto(media=image, caption=text, parse_mode="HTML"),
                reply_markup=markup,
            )
            return
        except Exception:
            pass

    try:
        if call.message.photo:
            await call.message.delete()
            await _send_best_location_carousel(call.message, events, index)
        else:
            await call.message.edit_text(text, reply_markup=markup, parse_mode="HTML")
    except Exception:
        await _send_best_location_carousel(call.message, events, index)


def _nav_kb():
    kb = InlineKeyboardBuilder()
    kb.button(text="🎟 Забронировать места", callback_data="book")
    kb.button(text="💳 Купить билет", callback_data="buy_ticket")
    kb.button(text="🎭 Наши форматы ШОУ", callback_data="formats")
    kb.button(text="📍 Наши площадки", callback_data="venues")
    kb.button(text="📋 Правила посещения шоу", callback_data="rules")
    kb.button(text="💬 Задать вопрос менеджеру", url=MANAGER_LINK)
    kb.button(text="📢 Заглянуть на наш канал анонсов", url=CHANNEL_LINK)
    kb.adjust(1)
    return kb.as_markup()


async def delete_linked_venue_album(call: CallbackQuery):
    for message_id in _VENUE_ALBUM_MESSAGE_IDS.pop(call.message.message_id, []):
        try:
            await call.bot.delete_message(call.message.chat.id, message_id)
        except Exception:
            pass


async def _delete_previous_menu_message(call: CallbackQuery):
    text = call.message.text or call.message.caption or ""
    if WELCOME_MARKER in text:
        return
    forget_booking_nav(call.message.chat.id, call.message.message_id)
    await delete_linked_venue_album(call)
    try:
        await call.message.delete()
    except Exception:
        pass


@router.callback_query(lambda c: c.data == "formats")
async def show_formats(call: CallbackQuery):
    await delete_booking_nav(call.bot, call.message.chat.id)
    await _delete_previous_menu_message(call)
    await send_all_formats(call.message)
    await call.answer()


@router.callback_query(lambda c: c.data == "venues")
async def show_venues(call: CallbackQuery):
    await _delete_previous_menu_message(call)

    linked_ids: list[int] = []
    # Сначала три блока площадок, в конце общий текст + кнопки меню
    for card in VENUE_CARDS:
        path = os.path.join(PHOTOS_DIR, card["file"])
        if os.path.exists(path):
            try:
                photo_msg = await call.message.answer_photo(photo=FSInputFile(path))
                linked_ids.append(photo_msg.message_id)
            except Exception:
                logger.exception("Failed to send venue photo %s", card["file"])

        # Подпись к фото не умеет полноценный Rich — текст площадки отдельным Rich-сообщением
        text_msg = await _send_rich_or_html(
            call.message,
            rich_html=card["rich_html"],
            fallback_html=card["fallback_html"],
        )
        if text_msg:
            linked_ids.append(text_msg.message_id)

    menu = await _send_rich_or_html(
        call.message,
        rich_html=VENUES_OUTRO_RICH_HTML,
        fallback_html=VENUES_OUTRO_TEXT,
        reply_markup=_nav_kb(),
    )
    if menu:
        _VENUE_ALBUM_MESSAGE_IDS[menu.message_id] = linked_ids
    await call.answer()


@router.callback_query(lambda c: c.data == "rules")
async def show_rules(call: CallbackQuery):
    await _delete_previous_menu_message(call)
    await call.message.answer(RULES_TEXT, reply_markup=_nav_kb(), parse_mode="HTML")
    await call.answer()


def _all_formats_kb(*, from_deep_link: bool = False):
    kb = InlineKeyboardBuilder()
    kb.button(text="STANDUP BEST", callback_data="best")
    kb.button(text="Хитлото", callback_data="hitloto")
    kb.button(text="StandUp Проверка материала", callback_data="check")
    # По deep link нет «предыдущего» экрана — сразу в главное меню
    back_text = "В главное меню" if from_deep_link else "◀️ Назад в меню"
    kb.button(text=back_text, callback_data="main_menu")
    kb.adjust(1)
    return kb.as_markup()


def _paid_formats_kb():
    kb = InlineKeyboardBuilder()
    kb.button(text="STANDUP BEST", callback_data="best")
    kb.button(text="Хитлото", callback_data="hitloto")
    kb.button(text="◀️ Назад в меню", callback_data="main_menu")
    kb.adjust(1)
    return kb.as_markup()


async def send_all_formats(message, *, from_deep_link: bool = False):
    """Все форматы: кнопка «Наши форматы ШОУ» и deep link ?start=quick_booking."""
    # Приветствие — только по deep link; из меню сразу «Наши форматы»
    if from_deep_link:
        rich_html = QUICK_BOOKING_GREETING_RICH + FORMATS_RICH_HTML
        fallback_html = QUICK_BOOKING_GREETING_TEXT + FORMATS_TEXT
    else:
        rich_html = FORMATS_RICH_HTML
        fallback_html = FORMATS_TEXT
    await _send_rich_or_html(
        message,
        rich_html=rich_html,
        fallback_html=fallback_html,
        reply_markup=_all_formats_kb(from_deep_link=from_deep_link),
    )


async def send_buy_ticket_formats(message):
    """Платные форматы: кнопка «Купить билет» — только BEST и Хитлото."""
    await _send_rich_or_html(
        message,
        rich_html=BUY_TICKET_RICH_HTML,
        fallback_html=BUY_TICKET_TEXT,
        reply_markup=_paid_formats_kb(),
    )


@router.callback_query(lambda c: c.data == "book")
async def book(call: CallbackQuery):
    """Бесплатная бронь: сразу экран Проверки материала."""
    from bot.handlers.booking import check_format_entry

    await delete_booking_nav(call.bot, call.message.chat.id)
    await _delete_previous_menu_message(call)
    await check_format_entry(call.message)
    await call.answer()


@router.callback_query(lambda c: c.data == "buy_ticket")
async def buy_ticket(call: CallbackQuery):
    await delete_booking_nav(call.bot, call.message.chat.id)
    await _delete_previous_menu_message(call)
    await send_buy_ticket_formats(call.message)
    await call.answer()


async def best_format_entry(message):
    """Точка входа в платную ветку BEST (меню или deep link afisha_plat)."""
    events = await load_events("best")
    if not events:
        kb = InlineKeyboardBuilder()
        kb.button(text="💬 Задать вопрос менеджеру", url=MANAGER_LINK)
        kb.button(text="◀️ Назад в меню", callback_data="main_menu")
        kb.adjust(1)
        await message.answer(
            "Пока нет актуальных мероприятий <b>StandUp BEST</b>. "
            "Можно уточнить расписание у менеджера 👇",
            reply_markup=kb.as_markup(),
            parse_mode="HTML",
        )
        return

    kb = InlineKeyboardBuilder()
    kb.button(text="📅 Выбрать по дате", callback_data="best_dates")
    kb.button(text="📍 Выбор по локации", callback_data="best_venues")
    kb.button(text="◀️ Назад в меню", callback_data="main_menu")
    kb.adjust(1)
    await _answer_with_format_photo(
        message,
        "Привет 😊 Я помогу тебе выбрать билеты на <b>StandUp BEST</b> "
        "от Moscow StandUp Show 🎤\n\nВыбирай формат поиска мероприятий 👇",
        reply_markup=kb.as_markup(),
        parse_mode="HTML",
        track_nav=True,
    )


@router.callback_query(lambda c: c.data == "best")
async def best_format(call: CallbackQuery):
    await _delete_previous_menu_message(call)
    await best_format_entry(call.message)
    await call.answer()


@router.callback_query(lambda c: c.data == "best_dates")
async def best_dates(call: CallbackQuery):
    await _delete_previous_menu_message(call)
    await _answer_with_format_photo(
        call.message, "Выбирай дату 👇", reply_markup=await _best_dates_kb(), track_nav=True
    )
    await call.answer()


@router.callback_query(lambda c: c.data.startswith("best_dates_page_"))
async def best_dates_page(call: CallbackQuery):
    page = int(call.data.replace("best_dates_page_", "", 1))
    markup = await _best_dates_kb(page)
    try:
        await call.message.edit_reply_markup(reply_markup=markup)
    except Exception:
        sent = await call.message.answer("Выбирай дату 👇", reply_markup=markup)
        remember_booking_nav(call.message.chat.id, sent.message_id)
    await call.answer()


@router.callback_query(lambda c: c.data == "best_venues")
async def best_venues(call: CallbackQuery):
    await _delete_previous_menu_message(call)
    await _answer_with_format_photo(
        call.message, "Выбирай локацию 👇", reply_markup=await _best_venues_kb(), track_nav=True
    )
    await call.answer()


@router.callback_query(lambda c: c.data.startswith("best_venue_"))
async def best_venue_events(call: CallbackQuery):
    await _delete_previous_menu_message(call)
    venue = call.data.replace("best_venue_", "", 1)
    events = sorted(
        [e for e in await load_events("best") if e["location"] == venue],
        key=_event_sort_key,
    )
    if not events:
        await call.message.answer("На этой площадке пока нет актуальных мероприятий.", reply_markup=await _best_venues_kb())
        await call.answer()
        return

    await _send_best_location_carousel(call.message, events, 0)
    await call.answer()


@router.callback_query(lambda c: c.data.startswith("best_loc_carousel_"))
async def best_location_carousel(call: CallbackQuery):
    payload = call.data.replace("best_loc_carousel_", "", 1)
    event_id, direction = payload.rsplit("_", 1)
    all_events = await load_events("best")
    current = _best_event_by_id(all_events, event_id)
    if not current:
        await call.answer("Мероприятие уже недоступно", show_alert=True)
        return

    events = sorted(
        [e for e in all_events if e["location"] == current.get("location")],
        key=_event_sort_key,
    )
    current_index = next((i for i, e in enumerate(events) if str(e["id"]) == event_id), 0)
    new_index = current_index + (1 if direction == "next" else -1)
    new_index = max(0, min(new_index, len(events) - 1))
    await _edit_best_location_carousel(call, events, new_index)
    await call.answer()


@router.callback_query(lambda c: c.data == "best_carousel_position")
async def best_carousel_position(call: CallbackQuery):
    await call.answer()


@router.callback_query(lambda c: c.data.startswith("best_vdate_"))
async def best_venue_date(call: CallbackQuery):
    await _delete_previous_menu_message(call)
    payload = call.data.replace("best_vdate_", "", 1)
    date, venue = payload.split("_", 1)
    events = sorted(
        [e for e in await load_events("best") if e["location"] == venue and e["date"] == date],
        key=_event_sort_key,
    )
    if not events:
        await call.message.answer("Это мероприятие уже прошло 😊", reply_markup=await _best_venues_kb())
        await call.answer()
        return
    if len(events) == 1:
        await _send_best_event_card(call.message, events[0], back_callback=f"best_venue_{venue}")
        await call.answer()
        return

    kb = InlineKeyboardBuilder()
    for event in events:
        kb.button(
            text=f"{event['time']} — {event['location']}",
            callback_data=f"best_event_{event['id']}_venue_{venue}",
        )
    kb.button(text="◀️ Назад", callback_data=f"best_venue_{venue}")
    kb.adjust(1)
    await call.message.answer("На эту дату несколько мероприятий, выбери нужное 👇", reply_markup=kb.as_markup())
    await call.answer()


@router.callback_query(lambda c: c.data.startswith("best_date_"))
async def best_date(call: CallbackQuery):
    await _delete_previous_menu_message(call)
    date = call.data.replace("best_date_", "", 1)
    events = [e for e in await load_events("best") if e["date"] == date]
    if not events:
        await call.message.answer("Это мероприятие уже прошло 😊 Выбери новую дату!", reply_markup=await _best_dates_kb())
        await call.answer()
        return
    if len(events) == 1:
        await _send_best_event_card(call.message, events[0], back_callback="best_dates")
        await call.answer()
        return

    kb = InlineKeyboardBuilder()
    for event in events:
        kb.button(
            text=f"🕐 {event['time']} — {event['location']}",
            callback_data=f"best_event_{event['id']}_date_{date}",
        )
    kb.button(text="◀️ Назад к датам", callback_data="best_dates")
    kb.adjust(1)
    await call.message.answer("На эту дату несколько мероприятий, выбери нужное 👇", reply_markup=kb.as_markup())
    await call.answer()


@router.callback_query(lambda c: c.data.startswith("best_event_"))
async def best_event(call: CallbackQuery):
    await _delete_previous_menu_message(call)
    event_id, back_callback = _parse_best_event_callback(call.data)
    event = _best_event_by_id(await load_events("best"), event_id)
    if event:
        await _send_best_event_card(call.message, event, back_callback=back_callback)
    else:
        await call.message.answer("Мероприятие уже прошло 😊 Выбери новую дату!", reply_markup=await _best_dates_kb())
    await call.answer()


async def _hitloto_dates_kb():
    events = await load_events("hitloto")
    dates = sorted(set(e["date"] for e in events), key=lambda d: datetime.strptime(d, "%d.%m.%Y"))
    kb = InlineKeyboardBuilder()
    for date in dates:
        try:
            d = datetime.strptime(date, "%d.%m.%Y")
            label = d.strftime("%d ") + MONTHS[d.strftime("%B")]
        except Exception:
            label = date
        kb.button(text=label, callback_data=f"hitloto_date_{date}")
    kb.button(text="◀️ Назад в меню", callback_data="main_menu")
    if dates:
        widths = [2] * (len(dates) // 2)
        if len(dates) % 2:
            widths.append(1)
        widths.append(1)
        kb.adjust(*widths)
    else:
        kb.adjust(1)
    return kb.as_markup()


async def _hitloto_venues_kb():
    events = await load_events("hitloto")
    venues = sorted(set(e["location"] for e in events))
    kb = InlineKeyboardBuilder()
    for venue in venues:
        kb.button(text=venue, callback_data=f"hitloto_venue_{venue}")
    kb.button(text="📅 Выбор по дате", callback_data="hitloto_dates")
    kb.button(text="◀️ Назад в меню", callback_data="main_menu")
    kb.adjust(*([1] * len(venues)), 1, 1)
    return kb.as_markup()


def _hitloto_event_by_id(events, event_id):
    return next((e for e in events if str(e["id"]) == event_id), None)


def _parse_hitloto_event_callback(data):
    payload = data.replace("hitloto_event_", "", 1)
    if "_date_" in payload:
        event_id, date = payload.split("_date_", 1)
        return event_id, f"hitloto_date_{date}"
    if "_venue_" in payload:
        event_id, venue = payload.split("_venue_", 1)
        return event_id, f"hitloto_venue_{venue}"
    return payload, "hitloto_dates"


def _hitloto_event_text(event):
    lines = _host_lines(event.get("host") or "")
    title = "Ведущие" if len(lines) > 1 else "Ведущий"
    return _best_event_text(event, host_title=title)


async def _send_hitloto_event_card(message, event, back_callback="hitloto_dates"):
    kb = InlineKeyboardBuilder()
    payment_url = event.get("payment_url") or ""
    if payment_url:
        kb.button(text="🎟 Купить билет", url=payment_url)
    else:
        kb.button(text="💬 Задать вопрос менеджеру", url=MANAGER_LINK)
    kb.button(text="◀️ Назад", callback_data=back_callback)
    kb.adjust(1)
    text = _hitloto_event_text(event)
    image = event.get("image") or ""
    if image:
        try:
            await message.answer_photo(photo=image, caption=text, reply_markup=kb.as_markup(), parse_mode="HTML")
            return
        except Exception:
            try:
                await message.answer_photo(photo=image)
            except Exception:
                pass
    await message.answer(text, reply_markup=kb.as_markup(), parse_mode="HTML")


async def hitloto_format_entry(message):
    events = await load_events("hitloto")
    if not events:
        kb = InlineKeyboardBuilder()
        kb.button(text="💬 Задать вопрос менеджеру", url=MANAGER_LINK)
        kb.button(text="◀️ Назад в меню", callback_data="main_menu")
        kb.adjust(1)
        await message.answer(
            "Пока нет актуальных мероприятий <b>Хитлото</b>. "
            "Можно уточнить расписание у менеджера 👇",
            reply_markup=kb.as_markup(),
            parse_mode="HTML",
        )
        return

    # Сразу выбор даты — для Хитлото выбор по локации не нужен
    await _answer_with_hitloto_photo(
        message,
        "Привет 😊 Я помогу тебе выбрать билеты на <b>Хитлото</b> "
        "от Moscow StandUp Show 🎤\n\nВыбирай дату 👇",
        reply_markup=await _hitloto_dates_kb(),
        parse_mode="HTML",
        track_nav=True,
    )


@router.callback_query(lambda c: c.data == "hitloto")
async def hitloto_format(call: CallbackQuery):
    await _delete_previous_menu_message(call)
    await hitloto_format_entry(call.message)
    await call.answer()


@router.callback_query(lambda c: c.data == "hitloto_dates")
async def hitloto_dates(call: CallbackQuery):
    await _delete_previous_menu_message(call)
    await _answer_with_hitloto_photo(
        call.message, "Выбирай дату 👇", reply_markup=await _hitloto_dates_kb(), track_nav=True
    )
    await call.answer()


@router.callback_query(lambda c: c.data == "hitloto_venues")
async def hitloto_venues(call: CallbackQuery):
    await _delete_previous_menu_message(call)
    await _answer_with_hitloto_photo(
        call.message, "Выбирай локацию 👇", reply_markup=await _hitloto_venues_kb(), track_nav=True
    )
    await call.answer()


@router.callback_query(lambda c: c.data.startswith("hitloto_venue_"))
async def hitloto_venue_events(call: CallbackQuery):
    await _delete_previous_menu_message(call)
    venue = call.data.replace("hitloto_venue_", "", 1)
    events = sorted(
        [e for e in await load_events("hitloto") if e["location"] == venue],
        key=_event_sort_key,
    )
    if len(events) == 1:
        await _send_hitloto_event_card(call.message, events[0], back_callback="hitloto_venues")
        await call.answer()
        return

    dates = sorted(set(e["date"] for e in events), key=lambda d: datetime.strptime(d, "%d.%m.%Y"))
    kb = InlineKeyboardBuilder()
    for date in dates:
        try:
            d = datetime.strptime(date, "%d.%m.%Y")
            label = d.strftime("%d ") + MONTHS[d.strftime("%B")]
        except Exception:
            label = date
        kb.button(text=label, callback_data=f"hitloto_vdate_{date}_{venue}")
    kb.button(text="📍 Назад к выбору локации", callback_data="hitloto_venues")
    widths = [2] * (len(dates) // 2)
    if len(dates) % 2:
        widths.append(1)
    widths.append(1)
    kb.adjust(*widths)
    sent = await call.message.answer(f"Мероприятия в {venue} 👇", reply_markup=kb.as_markup())
    remember_booking_nav(call.message.chat.id, sent.message_id)
    await call.answer()


@router.callback_query(lambda c: c.data.startswith("hitloto_vdate_"))
async def hitloto_venue_date(call: CallbackQuery):
    await _delete_previous_menu_message(call)
    payload = call.data.replace("hitloto_vdate_", "", 1)
    date, venue = payload.split("_", 1)
    events = sorted(
        [e for e in await load_events("hitloto") if e["location"] == venue and e["date"] == date],
        key=_event_sort_key,
    )
    if not events:
        await call.message.answer("Это мероприятие уже прошло 😊", reply_markup=await _hitloto_venues_kb())
        await call.answer()
        return
    if len(events) == 1:
        await _send_hitloto_event_card(call.message, events[0], back_callback=f"hitloto_venue_{venue}")
        await call.answer()
        return

    kb = InlineKeyboardBuilder()
    for event in events:
        kb.button(
            text=f"{event['time']} — {event['location']}",
            callback_data=f"hitloto_event_{event['id']}_venue_{venue}",
        )
    kb.button(text="◀️ Назад", callback_data=f"hitloto_venue_{venue}")
    kb.adjust(1)
    await call.message.answer("На эту дату несколько мероприятий, выбери нужное 👇", reply_markup=kb.as_markup())
    await call.answer()


@router.callback_query(lambda c: c.data.startswith("hitloto_date_"))
async def hitloto_date(call: CallbackQuery):
    await _delete_previous_menu_message(call)
    date = call.data.replace("hitloto_date_", "", 1)
    events = [e for e in await load_events("hitloto") if e["date"] == date]
    if not events:
        await call.message.answer("Это мероприятие уже прошло 😊 Выбери новую дату!", reply_markup=await _hitloto_dates_kb())
        await call.answer()
        return
    if len(events) == 1:
        await _send_hitloto_event_card(call.message, events[0], back_callback="hitloto_dates")
        await call.answer()
        return

    kb = InlineKeyboardBuilder()
    for event in events:
        kb.button(
            text=f"🕐 {event['time']} — {event['location']}",
            callback_data=f"hitloto_event_{event['id']}_date_{date}",
        )
    kb.button(text="◀️ Назад к датам", callback_data="hitloto_dates")
    kb.adjust(1)
    await call.message.answer("На эту дату несколько мероприятий, выбери нужное 👇", reply_markup=kb.as_markup())
    await call.answer()


@router.callback_query(lambda c: c.data.startswith("hitloto_event_"))
async def hitloto_event(call: CallbackQuery):
    await _delete_previous_menu_message(call)
    event_id, back_callback = _parse_hitloto_event_callback(call.data)
    event = _hitloto_event_by_id(await load_events("hitloto"), event_id)
    if event:
        await _send_hitloto_event_card(call.message, event, back_callback=back_callback)
    else:
        await call.message.answer("Мероприятие уже прошло 😊 Выбери новую дату!", reply_markup=await _hitloto_dates_kb())
    await call.answer()


