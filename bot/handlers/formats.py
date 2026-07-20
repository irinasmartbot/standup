import os
import random
from datetime import datetime
from html import escape

from aiogram import Router
from aiogram.types import CallbackQuery, FSInputFile
from aiogram.utils.media_group import MediaGroupBuilder
from aiogram.utils.keyboard import InlineKeyboardBuilder
from bot.config import MANAGER_LINK, CHANNEL_LINK, TICKET_TEMPLATE
from bot.services.sheets import load_events
from bot.utils.ticket import MONTHS, format_date
from bot.utils.nav_messages import remember_booking_nav, forget_booking_nav, delete_booking_nav

router = Router()

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
PHOTOS_DIR = os.path.join(_PROJECT_ROOT, "фото")
WELCOME_MARKER = "Здесь ты сможешь узнать о нас побольше и забронировать места:"
VENUE_PHOTO_FILES = {"temple_bar.jpg", "escobar.jpg", "nebar.jpg"}
_VENUE_ALBUM_MESSAGE_IDS = {}

FORMATS_TEXT = """🎭 <b>Наши форматы шоу:</b>

<b>Формат StandUp BEST:</b>
Только лучший, уже проверенный стэндап материал от троих комиков, именитых участников многочисленных телевизионных проектов. Вы не услышите ни одной несмешной шутки, только BEST!!
Билеты - от 500 рублей.

<b>Формат Хитлото от Moscow StandUp Show:</b>
Музыкальное лото в компании стендап-комика! Веселись, пой, пей, зачеркивай прозвучавшие песни в своём бланке, и выигрывай призы 🔥
Билеты - от 990 рублей.

<b>Формат StandUp Проверка материала:</b>
5-7 опытных комиков, участников известных проектов ТНТ и YouTube, рассказывают по 10-15 минут свежих, но не проверенных шуток. Вы услышите настоящий эксклюзив и поможете комикам понять, что смешно, а что стоит убрать из материала 🐒
Вход бесплатный."""

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


async def _best_dates_kb():
    events = await load_events("best")
    dates = sorted(set(e["date"] for e in events), key=lambda d: datetime.strptime(d, "%d.%m.%Y"))
    kb = InlineKeyboardBuilder()
    for date in dates:
        try:
            d = datetime.strptime(date, "%d.%m.%Y")
            label = d.strftime("%d ") + MONTHS[d.strftime("%B")]
        except Exception:
            label = date
        kb.button(text=label, callback_data=f"best_date_{date}")
    kb.button(text="📍 Выбор по площадкам", callback_data="best_venues")
    kb.button(text="◀️ Назад в меню", callback_data="main_menu")
    if dates:
        widths = [2] * (len(dates) // 2)
        if len(dates) % 2:
            widths.append(1)
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


def _best_event_text(event):
    parts = [
        f"<b>{format_date(event['date'])}</b>",
        escape(event.get("weekday") or ""),
        "",
        f"<b>{escape(event.get('time') or '')}</b>",
        escape(event.get("address") or ""),
        escape(event.get("description") or ""),
    ]
    return "\n".join(parts)


async def _send_best_event_card(message, event, back_callback="best_dates"):
    kb = InlineKeyboardBuilder()
    payment_url = event.get("payment_url") or ""
    if payment_url:
        kb.button(text="🎟 Купить билет", url=payment_url)
    else:
        kb.button(text="💬 Задать вопрос менеджеру", url=MANAGER_LINK)
    kb.button(text="🎤 Кто выступает", callback_data=f"best_speakers_{event['id']}")
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


def _nav_kb():
    kb = InlineKeyboardBuilder()
    kb.button(text="🎟 Забронировать места", callback_data="book")
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
    kb = InlineKeyboardBuilder()
    kb.button(text="🎟 Бронь Формат StandUp BEST", callback_data="best")
    kb.button(text="🎟 Бронь Формат Хитлото", callback_data="hitloto")
    kb.button(text="🎟 Бронь Формат StandUp Проверка материала", callback_data="check")
    kb.button(text="◀️ Назад в меню", callback_data="main_menu")
    kb.adjust(1)
    await call.message.answer(FORMATS_TEXT, reply_markup=kb.as_markup(), parse_mode="HTML")
    await call.answer()


@router.callback_query(lambda c: c.data == "venues")
async def show_venues(call: CallbackQuery):
    await _delete_previous_menu_message(call)
    photo_files = ["temple_bar.jpg", "escobar.jpg", "nebar.jpg"]
    text = """Мероприятия проходят в заведениях, где каждый найдёт что-то на свой вкус: для любителей вкусно покушать — рестораны с изысканной кухней разных народов мира, для поклонников шумных вечеринок — бары, для любителей попеть — заведения с караоке, везде можно остаться после шоу.

Наши площадки:

<b>Temple Bar</b> - это английская респектабельность, ирландское жизнелюбие и русское гостеприимство в одном ресторане, где каждый гость будет чувствовать демократическую атмосферу, и сможет насладиться великолепными стейками, большим ассортиментом коктейлей, а также отменными блюдами из мяса и овощей на мангале.

<b>Escobar</b> - бар с неординарной кухней, расположенный в комплексе исторических зданий 18-19 веков, брутальный дизайн в эстетике фильмов Квентина Тарантино, с легким оттенком латиноамериканской расслабленности.

<b>Небар</b> - один из самых популярных и громких баров столицы с уникальным стилем. Авторская коктейльная карта для тех, кто любит эксперименты, насчитывает 13 сезонных коктейлей на любой вкус, названных в честь известных городов мира."""
    kb = _nav_kb()

    media = MediaGroupBuilder()
    for photo_file in photo_files:
        path = os.path.join(PHOTOS_DIR, photo_file)
        if os.path.exists(path):
            media.add_photo(FSInputFile(path))
    album = media.build()
    album_messages = []
    if album:
        album_messages = await call.message.answer_media_group(media=album)

    text_message = await call.message.answer(text, parse_mode="HTML", reply_markup=kb)
    if album_messages:
        _VENUE_ALBUM_MESSAGE_IDS[text_message.message_id] = [
            message.message_id for message in album_messages
        ]
    await call.answer()


@router.callback_query(lambda c: c.data == "rules")
async def show_rules(call: CallbackQuery):
    await _delete_previous_menu_message(call)
    await call.message.answer(RULES_TEXT, reply_markup=_nav_kb(), parse_mode="HTML")
    await call.answer()


@router.callback_query(lambda c: c.data == "book")
async def book(call: CallbackQuery):
    await delete_booking_nav(call.bot, call.message.chat.id)
    await _delete_previous_menu_message(call)
    kb = InlineKeyboardBuilder()
    kb.button(text="STANDUP BEST", callback_data="best")
    kb.button(text="Хитлото", callback_data="hitloto")
    kb.button(text="StandUp Проверка материала", callback_data="check")
    kb.button(text="◀️ Назад в меню", callback_data="main_menu")
    kb.adjust(1)
    await call.message.answer(
        "Выбирай формат шоу 👇\n\n"
        "<b>Формат StandUp BEST:</b>\n"
        "Только лучший, уже проверенный стэндап материал от троих комиков, "
        "именитых участников многочисленных телевизионных проектов. "
        "Вы не услышите ни одной несмешной шутки, только BEST!!\n"
        "Билеты - от 500 рублей.\n\n"
        "<b>Формат Хитлото от Moscow StandUp Show:</b>\n"
        "Музыкальное лото в компании стендап-комика! Веселись, пой, пей, "
        "зачеркивай прозвучавшие песни в своём бланке, и выигрывай призы 🔥\n"
        "Билеты - от 990 рублей.\n\n"
        "<b>Формат StandUp проверка материала:</b>\n"
        "5-7 опытных комиков, участников известных проектов ТНТ и YouTube, "
        "рассказывают по 10-15 минут свежих, но не проверенных шуток, "
        "Вы услышите настоящий эксклюзив и поможете комикам понять, "
        "что смешно, а что стоит убрать из материала 🙈\n"
        "Вход бесплатный.",
        reply_markup=kb.as_markup(),
        parse_mode="HTML",
    )
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
    if len(events) == 1:
        await _send_best_event_card(call.message, events[0], back_callback="best_venues")
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
        kb.button(text=label, callback_data=f"best_vdate_{date}_{venue}")
    kb.button(text="📍 Назад к выбору локации", callback_data="best_venues")
    widths = [2] * (len(dates) // 2)
    if len(dates) % 2:
        widths.append(1)
    widths.append(1)
    kb.adjust(*widths)
    sent = await call.message.answer(f"Мероприятия в {venue} 👇", reply_markup=kb.as_markup())
    remember_booking_nav(call.message.chat.id, sent.message_id)
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


@router.callback_query(lambda c: c.data.startswith("best_speakers_"))
async def best_speakers(call: CallbackQuery):
    event_id = call.data.replace("best_speakers_", "", 1)
    event = next((e for e in await load_events("best") if str(e["id"]) == event_id), None)
    host = (event or {}).get("host") or ""
    if not host:
        await call.answer(f"По составу комиков напишите менеджеру {_manager_username()}", show_alert=True)
        return
    if len(host) > 190:
        host = host[:187].rstrip() + "..."
    await call.answer(host, show_alert=True)


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
    kb.button(text="📍 Выбор по площадкам", callback_data="hitloto_venues")
    kb.button(text="◀️ Назад в меню", callback_data="main_menu")
    if dates:
        widths = [2] * (len(dates) // 2)
        if len(dates) % 2:
            widths.append(1)
        widths.extend([1, 1])
        kb.adjust(*widths)
    else:
        kb.adjust(1, 1)
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
    return _best_event_text(event)


async def _send_hitloto_event_card(message, event, back_callback="hitloto_dates"):
    kb = InlineKeyboardBuilder()
    payment_url = event.get("payment_url") or ""
    if payment_url:
        kb.button(text="Купить", url=payment_url)
    else:
        kb.button(text="💬 Задать вопрос менеджеру", url=MANAGER_LINK)
    kb.button(text="Ведущие", callback_data=f"hitloto_speakers_{event['id']}")
    kb.button(text="Назад", callback_data="hitloto_dates")
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

    kb = InlineKeyboardBuilder()
    kb.button(text="📅 Выбрать по дате", callback_data="hitloto_dates")
    kb.button(text="📍 Выбор по локации", callback_data="hitloto_venues")
    kb.button(text="◀️ Назад в меню", callback_data="main_menu")
    kb.adjust(1)
    await _answer_with_hitloto_photo(
        message,
        "Привет 😊 Я помогу тебе выбрать билеты на <b>Хитлото</b> "
        "от Moscow StandUp Show 🎤\n\nВыбирай формат поиска мероприятий 👇",
        reply_markup=kb.as_markup(),
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


@router.callback_query(lambda c: c.data.startswith("hitloto_speakers_"))
async def hitloto_speakers(call: CallbackQuery):
    event_id = call.data.replace("hitloto_speakers_", "", 1)
    event = next((e for e in await load_events("hitloto") if str(e["id"]) == event_id), None)
    host = (event or {}).get("host") or ""
    if not host:
        await call.answer(f"По ведущему напишите менеджеру {_manager_username()}", show_alert=True)
        return
    if len(host) > 190:
        host = host[:187].rstrip() + "..."
    await call.answer(host, show_alert=True)
