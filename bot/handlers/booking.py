import os
import random
from datetime import datetime
from aiogram import Router, F
from aiogram.types import Message, CallbackQuery, FSInputFile, BufferedInputFile
from aiogram.types import ReplyKeyboardMarkup, KeyboardButton, ReplyKeyboardRemove
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.utils.keyboard import InlineKeyboardBuilder

from bot.config import bot, MANAGER_LINK, CHANNEL_LINK, MANAGER_PHONE, TICKET_TEMPLATE
from bot.db.crud import (
    get_booking, get_active_booking_by_id, get_booking_by_id, create_booking,
    update_booking_status, update_booking_guests, get_total_guests,
    save_ticket_message_id, save_confirm_message_id, get_last_phone,
    get_active_bookings_by_user,
)
from bot.services.sheets import load_events, get_event
from bot.utils.ticket import format_date, guests_word, generate_ticket, MONTHS, parse_event_datetime

router = Router()

# Корень проекта — два уровня выше bot/handlers/booking.py
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
PHOTOS_DIR = os.path.join(_PROJECT_ROOT, "фото")


def _random_check_photo():
    """Возвращает случайное фото из папки 'фото', исключая шаблон билета."""
    ticket_name = os.path.basename(TICKET_TEMPLATE)
    try:
        files = [
            f for f in os.listdir(PHOTOS_DIR)
            if f.lower().endswith((".jpg", ".jpeg", ".png")) and f != ticket_name
        ]
    except FileNotFoundError:
        files = []
    if files:
        return FSInputFile(os.path.join(PHOTOS_DIR, random.choice(files)))
    fallback = os.path.join(_PROJECT_ROOT, "check_photo.jpg")
    if os.path.exists(fallback):
        return FSInputFile(fallback)
    return None


def _manage_kb(booking_id):
    """Клавиатура управления бронью без кнопки 'Получить билет'."""
    kb = InlineKeyboardBuilder()
    kb.button(text="Отменить бронь", callback_data=f"cancel_confirm_{booking_id}")
    kb.button(text="Изменить дату", callback_data=f"change_date_{booking_id}")
    kb.button(text="Изменить количество гостей", callback_data=f"change_guests_confirm_{booking_id}")
    kb.button(text="💬 Задать вопрос менеджеру", url=MANAGER_LINK)
    kb.adjust(1)
    return kb.as_markup()


async def _remove_ticket_button(booking_id: int, chat_id: int):
    """Убирает кнопку 'Получить билет' из сохранённого сообщения подтверждения."""
    booking = get_booking_by_id(booking_id)
    if not booking:
        return
    confirm_message_id = booking[-1] if len(booking) > 15 else None
    if confirm_message_id:
        try:
            await bot.edit_message_reply_markup(
                chat_id=chat_id,
                message_id=confirm_message_id,
                reply_markup=None,
            )
        except Exception:
            pass


async def _delete_ticket(booking_id: int, chat_id: int):
    """Удаляет сообщение с билетом из чата если оно было сохранено."""
    booking = get_booking_by_id(booking_id)
    if not booking:
        return
    # ticket_message_id — предпоследняя колонка (перед confirm_message_id)
    ticket_message_id = booking[-2]
    if ticket_message_id:
        try:
            await bot.delete_message(chat_id=chat_id, message_id=ticket_message_id)
        except Exception:
            # Сообщение слишком старое или уже удалено — отправляем уведомление
            try:
                await bot.send_message(
                    chat_id=chat_id,
                    text="❌ Ваш электронный билет аннулирован в связи с изменением или отменой брони.",
                )
            except Exception:
                pass

BOOKING_RULES_TEXT = """📋 <b>Порядок посещения шоу:</b>

1. Сбор гостей начинается за полчаса до начала шоу

2. Рассадка осуществляется по мере прихода, чтобы занять лучшие места, приходите вовремя 👊 Возможна подсадка за один стол других гостей для небольших компаний.

3. Обратите внимание, что при посещении шоу заказ минимум одной позиции по меню является обязательным.

4. Если поменяются планы, пожалуйста, ОБЯЗАТЕЛЬНО ПРЕДУПРЕДИТЕ 😊 - у вас будет возможность отменить бронь или изменить дату при необходимости 👌"""


class BookingState(StatesGroup):
    waiting_name = State()
    waiting_phone = State()
    waiting_guests = State()
    waiting_new_guests = State()
    waiting_new_name = State()
    waiting_new_phone = State()


async def check_dates_kb():
    events = await load_events()
    dates = sorted(set(e["date"] for e in events))
    kb = InlineKeyboardBuilder()
    for date in dates:
        try:
            d = datetime.strptime(date, "%d.%m.%Y")
            label = d.strftime("%d ") + MONTHS[d.strftime("%B")]
        except Exception:
            label = date
        kb.button(text=label, callback_data=f"date_{date}")
    kb.button(text="📍 Выбор по площадке", callback_data="by_venue")
    # Даты по 2 в ряд, кнопка площадки — отдельной строкой
    n = len(dates)
    widths = [2] * (n // 2)
    if n % 2:
        widths.append(1)
    widths.append(1)
    kb.adjust(*widths)
    return kb.as_markup()


async def send_event_card(message, event):
    date_str = format_date(event["date"])
    text = f"{date_str}\n{event['weekday']}\n\n{event['time']}\n{event['address']}\n{event['description']}"
    kb = InlineKeyboardBuilder()
    kb.button(text="🎟 Забронировать билеты", callback_data=f"book_event_{event['date']}_{event['time']}")
    kb.button(text="📋 Правила бронирования", callback_data="booking_rules")
    kb.button(text="◀️ Назад", callback_data="check_dates")
    kb.adjust(1)
    try:
        await message.answer_photo(photo=event["image"], caption=text, reply_markup=kb.as_markup())
    except Exception:
        await message.answer(text, reply_markup=kb.as_markup())


@router.callback_query(lambda c: c.data == "check")
async def check_format(call: CallbackQuery):
    kb = InlineKeyboardBuilder()
    kb.button(text="📅 Выбрать по дате", callback_data="check_dates")
    kb.button(text="📍 Выбор по площадке", callback_data="by_venue")
    kb.adjust(1)
    photo = _random_check_photo()
    try:
        if photo:
            await call.message.answer_photo(
                photo=photo,
                caption=(
                    "Привет! 😊 Я помогу тебе забронировать места на <b>Проверку материала</b> "
                    "от Moscow StandUp Show 🎤\n\nВыбирай формат поиска мероприятий 👇"
                ),
                reply_markup=kb.as_markup(),
                parse_mode="HTML",
            )
        else:
            await call.message.answer(
                "Привет! 😊 Выбирай формат поиска мероприятий 👇",
                reply_markup=kb.as_markup(),
            )
    except Exception:
        await call.message.answer(
            "Привет! 😊 Выбирай формат поиска мероприятий 👇",
            reply_markup=kb.as_markup(),
        )
    await call.answer()


@router.callback_query(lambda c: c.data == "check_dates")
async def check_dates(call: CallbackQuery):
    kb = await check_dates_kb()
    photo = _random_check_photo()
    try:
        if photo:
            await call.message.answer_photo(
                photo=photo,
                caption="Выбирай дату 👇",
                reply_markup=kb,
            )
        else:
            await call.message.answer("Выбирай дату 👇", reply_markup=kb)
    except Exception:
        await call.message.answer("Выбирай дату 👇", reply_markup=kb)
    await call.answer()


@router.callback_query(lambda c: c.data == "by_venue")
async def by_venue(call: CallbackQuery):
    events = await load_events()
    venues = sorted(set(e["location"] for e in events))
    kb = InlineKeyboardBuilder()
    for venue in venues:
        kb.button(text=venue, callback_data=f"venue_{venue}")
    kb.button(text="📅 Выбор по дате", callback_data="check_dates")
    # Площадки по 1 в ряд, кнопка "по дате" — отдельной строкой
    kb.adjust(*([1] * len(venues)), 1)
    await call.message.answer("Выбирай локацию 👇", reply_markup=kb.as_markup())
    await call.answer()


@router.callback_query(F.data.startswith("venue_"))
async def venue_events(call: CallbackQuery):
    venue = call.data.replace("venue_", "")
    events = await load_events()
    filtered = sorted(
        [e for e in events if e["location"] == venue],
        key=lambda x: datetime.strptime(x["date"], "%d.%m.%Y"),
    )
    kb = InlineKeyboardBuilder()
    for e in filtered:
        try:
            d = datetime.strptime(e["date"], "%d.%m.%Y")
            label = f"📅 {d.strftime('%d ') + MONTHS[d.strftime('%B')]} ({e['weekday']}) {e['time']}"
        except Exception:
            label = e["date"]
        kb.button(text=label, callback_data=f"event_{e['date']}_{e['time']}")
    kb.button(text="📅 Выбор по дате", callback_data="check_dates")
    kb.adjust(1)
    await call.message.answer(f"Мероприятия в {venue} 👇", reply_markup=kb.as_markup())
    await call.answer()


@router.callback_query(F.data.startswith("date_"))
async def show_event(call: CallbackQuery):
    date = call.data.replace("date_", "")
    events = await load_events()
    day_events = [e for e in events if e["date"] == date]
    if not day_events:
        await call.message.answer("Это мероприятие уже прошло 😊 Выбери новую дату!", reply_markup=await check_dates_kb())
        await call.answer()
        return
    if len(day_events) == 1:
        await send_event_card(call.message, day_events[0])
    else:
        kb = InlineKeyboardBuilder()
        for e in day_events:
            kb.button(text=f"🕐 {e['time']} — {e['location']}", callback_data=f"event_{e['date']}_{e['time']}")
        kb.adjust(1)
        await call.message.answer("На эту дату несколько мероприятий, выбери нужное 👇", reply_markup=kb.as_markup())
    await call.answer()


@router.callback_query(F.data.startswith("event_"))
async def show_specific_event(call: CallbackQuery):
    event_date, event_time = call.data.replace("event_", "", 1).split("_", 1)
    events = await load_events()
    event = next((e for e in events if e["date"] == event_date and e["time"] == event_time), None)
    if event:
        await send_event_card(call.message, event)
    await call.answer()


@router.callback_query(lambda c: c.data == "booking_rules")
async def show_booking_rules(call: CallbackQuery):
    await call.message.answer(BOOKING_RULES_TEXT, parse_mode="HTML")
    await call.answer()


@router.callback_query(F.data.startswith("book_event_"))
async def start_booking(call: CallbackQuery, state: FSMContext):
    event_date, event_time = call.data.replace("book_event_", "", 1).split("_", 1)
    try:
        if datetime.strptime(event_date, "%d.%m.%Y").date() < datetime.now().date():
            await call.message.answer("Это мероприятие уже прошло 😊 Выбери новую дату!", reply_markup=await check_dates_kb())
            await call.answer()
            return
    except Exception:
        pass

    existing = get_booking(call.from_user.id, event_date, event_time)
    if existing:
        date_str = format_date(event_date)
        kb = InlineKeyboardBuilder()
        kb.button(text="Отменить бронь", callback_data=f"cancel_confirm_{existing[0]}")
        kb.button(text="Изменить дату", callback_data=f"change_date_{existing[0]}")
        kb.button(text="💬 Задать вопрос менеджеру", url=MANAGER_LINK)
        kb.adjust(1)
        await call.message.answer(
            f"⚠️ ВНИМАНИЕ, мы уже внесли Вас в списки гостей:\n\n"
            f"Дата: {date_str}\n"
            f"Время: {existing[6]}\n"
            f"Локация: {existing[8]}\n"
            f"Количество гостей: {existing[9]} чел.\n\n"
            f"Вы не можете забронировать повторный билет на данное мероприятие",
            reply_markup=kb.as_markup(),
        )
        await call.answer()
        return

    await state.update_data(event_date=event_date, event_time=event_time)
    name = call.from_user.first_name or ""
    if call.from_user.last_name:
        name += f" {call.from_user.last_name}"
    await state.update_data(name=name)

    kb = InlineKeyboardBuilder()
    kb.button(text="Все верно 👌", callback_data="name_confirm")
    kb.button(text="Изменить", callback_data="name_change")
    kb.adjust(2)
    await call.message.answer(
        f"Для бронирования вам нужно заполнить некоторые данные\n\nВаше имя <b>{name}</b>, верно?",
        reply_markup=kb.as_markup(),
        parse_mode="HTML",
    )
    await call.answer()


def _phone_kb():
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="📱 Поделиться номером", request_contact=True)],
        ],
        resize_keyboard=True,
        one_time_keyboard=True,
    )


@router.callback_query(lambda c: c.data == "name_confirm")
async def name_confirmed(call: CallbackQuery, state: FSMContext):
    saved_phone = get_last_phone(call.from_user.id)
    if saved_phone:
        await state.update_data(phone=saved_phone)
        kb = InlineKeyboardBuilder()
        kb.button(text="✅ Да, использовать", callback_data="phone_use_saved")
        kb.button(text="✏️ Ввести другой номер", callback_data="phone_change")
        kb.adjust(1)
        await call.message.answer(
            f"Ваш номер телефона: <b>{saved_phone}</b>\nИспользовать его?",
            reply_markup=kb.as_markup(),
            parse_mode="HTML",
        )
    else:
        await call.message.answer("Поделитесь номером телефона или введите вручную:", reply_markup=_phone_kb())
        await state.set_state(BookingState.waiting_phone)
    await call.answer()


@router.callback_query(lambda c: c.data == "name_change")
async def name_change(call: CallbackQuery, state: FSMContext):
    await call.message.answer("Напишите, пожалуйста, ваше имя.")
    await state.set_state(BookingState.waiting_name)
    await call.answer()


@router.message(BookingState.waiting_name)
async def process_name(message: Message, state: FSMContext):
    await state.update_data(name=message.text)
    await message.answer("Поделитесь номером телефона или введите вручную:", reply_markup=_phone_kb())
    await state.set_state(BookingState.waiting_phone)


@router.message(BookingState.waiting_phone, F.contact)
async def process_phone_contact(message: Message, state: FSMContext):
    await state.update_data(phone=message.contact.phone_number)
    data = await state.get_data()
    await message.answer(
        f"{data.get('name', '')}, напишите пожалуйста цифрой, на какое количество человек бронируете?\n\n"
        f"<b>Внимание, бронь на один билет максимум 4 человека</b>",
        reply_markup=ReplyKeyboardRemove(),
        parse_mode="HTML",
    )
    await state.set_state(BookingState.waiting_guests)


@router.message(BookingState.waiting_phone)
async def process_phone_text(message: Message, state: FSMContext):
    await state.update_data(phone=message.text)
    data = await state.get_data()
    await message.answer(
        f"{data.get('name', '')}, напишите пожалуйста цифрой, на какое количество человек бронируете?\n\n"
        f"<b>Внимание, бронь на один билет максимум 4 человека</b>",
        reply_markup=ReplyKeyboardRemove(),
        parse_mode="HTML",
    )
    await state.set_state(BookingState.waiting_guests)


@router.callback_query(lambda c: c.data == "phone_use_saved")
async def phone_use_saved(call: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    name = data.get("name", "")
    await call.message.answer(
        f"{name}, напишите пожалуйста цифрой, на какое количество человек бронируете?\n\n"
        f"<b>Внимание, бронь на один билет максимум 4 человека</b>",
        reply_markup=ReplyKeyboardRemove(),
        parse_mode="HTML",
    )
    await state.set_state(BookingState.waiting_guests)
    await call.answer()


@router.callback_query(lambda c: c.data == "phone_change")
async def phone_change(call: CallbackQuery, state: FSMContext):
    await call.message.answer("Поделитесь номером телефона или введите вручную:", reply_markup=_phone_kb())
    await state.set_state(BookingState.waiting_phone)
    await call.answer()


@router.message(BookingState.waiting_guests)
async def process_guests(message: Message, state: FSMContext):
    if not message.text.isdigit():
        await message.answer("Пожалуйста, напишите цифрой количество гостей (от 1 до 4)")
        return
    guests = int(message.text)
    if guests < 1 or guests > 4:
        await message.answer("Максимум 4 человека на одну бронь. Напишите цифру от 1 до 4.")
        return

    data = await state.get_data()
    event_date = data.get("event_date")
    event_time = data.get("event_time")
    name = data.get("name", "")
    phone = data.get("phone", "")

    event = await get_event(event_date, event_time)
    if event:
        total = get_total_guests(event_date, event_time)
        if total + guests > event["max_seats"]:
            available = event["max_seats"] - total
            if available <= 0:
                await message.answer("К сожалению, на это мероприятие места закончились 😔 Выбери другую дату!", reply_markup=await check_dates_kb())
                await state.clear()
                return
            else:
                await message.answer(f"К сожалению, доступно только {available} мест. Укажите меньшее количество гостей.")
                return

    event_address = event["address"] if event else ""
    event_location = event["location"] if event else ""
    booking_id = create_booking(
        message.from_user.id, message.from_user.username or "",
        name, phone, event_date, event_time, event_address, event_location, guests,
    )

    date_str = format_date(event_date)
    await state.update_data(booking_id=booking_id)
    await state.set_state(None)

    try:
        days_until = (datetime.strptime(event_date, "%d.%m.%Y").date() - datetime.now().date()).days
    except Exception:
        days_until = 99

    kb = InlineKeyboardBuilder()
    if days_until <= 1:
        kb.button(text="🎟 Получить билет 🎟", callback_data=f"get_ticket_{booking_id}")
    kb.button(text="Отменить бронь", callback_data=f"cancel_confirm_{booking_id}")
    kb.button(text="Изменить дату", callback_data=f"change_date_{booking_id}")
    kb.button(text="Изменить количество гостей", callback_data=f"change_guests_confirm_{booking_id}")
    kb.button(text="💬 Задать вопрос менеджеру", url=MANAGER_LINK)
    kb.button(text="📢 Заглянуть на наш канал анонсов", url=CHANNEL_LINK)
    kb.adjust(1)

    if days_until <= 1:
        text = (
            f"Отлично! Мы внесли Вас в списки гостей:\n\n"
            f"<b>Дата:</b> {date_str} ({event['weekday'] if event else ''})\n"
            f"<b>Время:</b> {event_time}\n"
            f"<b>Локация:</b> {event_address}\n"
            f"<b>Количество гостей:</b> {guests} чел.\n\n"
            f"<b>❗ Важная информация — для того чтобы мы окончательно закрепили за Вами место ОБЯЗАТЕЛЬНО подтвердите бронь, нажав на кнопку «Получить билет»</b>\n\n"
            f"<b>❗ Внимание, если Вы не успеете подтвердить бронь, она будет аннулирована.</b>"
        )
    else:
        text = (
            f"Отлично! Мы внесли Вас в списки гостей:\n\n"
            f"<b>Дата:</b> {date_str} ({event['weekday'] if event else ''})\n"
            f"<b>Время:</b> {event_time}\n"
            f"<b>Локация:</b> {event_address}\n"
            f"<b>Количество гостей:</b> {guests} чел.\n\n"
            f"<b>❗ Внимание, за сутки до мероприятия Вам придёт сообщение-напоминание с подробностями и кнопкой «Получить билет». Обязательно нажмите кнопку, чтобы подтвердить бронь. Если Вы не успеете подтвердить бронь, она будет аннулирована.</b>\n\n"
            f"Если поменяются планы, обязательно предупредите 😊"
        )
    confirm_msg = await message.answer(text, reply_markup=kb.as_markup(), parse_mode="HTML")
    if days_until <= 1:
        save_confirm_message_id(booking_id, confirm_msg.message_id)


@router.callback_query(F.data.startswith("get_ticket_"))
async def get_ticket(call: CallbackQuery):
    booking_id = int(call.data.replace("get_ticket_", ""))
    booking = get_active_booking_by_id(booking_id)
    if not booking:
        await call.message.answer("Бронь не найдена или уже отменена.")
        await call.answer()
        return
    if booking[10] == "confirmed":
        await call.answer("Билет уже был выдан ранее.", show_alert=True)
        return

    name = booking[3]
    event_date = booking[5]
    event_time = booking[6]
    event_address = booking[7]
    event_location = booking[8]
    guests = booking[9]

    date_str = format_date(event_date)
    short_address = f"{event_location}, {event_address.split(',')[1] if ',' in event_address else event_address}"
    ticket_buf = generate_ticket(name, event_date, event_time, short_address, guests)
    update_booking_status(booking_id, "confirmed")

    ticket_msg = await call.message.answer_photo(
        photo=BufferedInputFile(ticket_buf.getvalue(), filename=f"ticket_{booking_id}.jpg"),
        caption=(
            f"Отлично!\n\nДанные по билету:\n\n"
            f"Ваше имя: {name}\n"
            f"Дата: {event_date}\n"
            f"Время: {event_time}\n"
            f"Место: {event_address}\n"
            f"Количество гостей: {guests_word(guests)}\n\n"
            f"Ждем вас на мероприятии ❤️"
        ),
    )
    save_ticket_message_id(booking_id, ticket_msg.message_id)
    await _remove_ticket_button(booking_id, call.from_user.id)
    await call.message.answer(
        f"Если поменяются планы, пожалуйста, ОБЯЗАТЕЛЬНО НАЖМИТЕ КНОПКУ «Отменить бронь» 😊\n\n"
        f"При возникновении вопросов - можно писать менеджеру @ccoverr (если срочно - звоните {MANAGER_PHONE})\n\n"
        f"И не забудь заглянуть на наш <a href='{CHANNEL_LINK}'>канал анонсов</a> (там часто дарят бесплатные билеты на платные шоу 😉)",
        reply_markup=_manage_kb(booking_id),
        parse_mode="HTML",
    )
    await call.answer()


async def _check_booking_actionable(booking_id: int, call: CallbackQuery):
    """
    Проверяет, что бронь активна и мероприятие ещё не прошло.
    Возвращает объект брони если всё ок, иначе None (и сам отправляет сообщение).
    """
    booking = get_active_booking_by_id(booking_id)
    if not booking:
        await call.answer("Эта бронь уже отменена или не найдена.", show_alert=True)
        return None

    event_dt = parse_event_datetime(booking[5], booking[6])
    if event_dt and event_dt < datetime.now():
        await call.answer()
        kb = InlineKeyboardBuilder()
        kb.button(text="📅 Посмотреть актуальные даты", callback_data="check_dates")
        kb.adjust(1)
        await call.message.answer(
            "К сожалению, это мероприятие уже прошло. "
            "Посмотри актуальное расписание 👇",
            reply_markup=kb.as_markup(),
        )
        return None

    return booking


def _multi_booking_text(bookings: list, question: str) -> str:
    """Текст с перечислением дат активных броней и уточняющим вопросом."""
    lines = []
    for b in bookings:
        lines.append(f"• {format_date(b[5])} {b[6]}")
    dates_str = "\n".join(lines)
    word = "брони" if len(bookings) in (2, 3, 4) else "броней"
    return f"У вас {len(bookings)} {word}:\n{dates_str}\n\n{question}"


# ─── ОТМЕНА БРОНИ ──────────────────────────────────────────────────────────────

@router.callback_query(F.data.startswith("cancel_confirm_"))
async def cancel_confirm(call: CallbackQuery):
    booking_id = call.data.replace("cancel_confirm_", "")
    this_booking = await _check_booking_actionable(int(booking_id), call)
    if not this_booking:
        return
    bookings = get_active_bookings_by_user(call.from_user.id)

    if len(bookings) > 1:
        kb = InlineKeyboardBuilder()
        for b in bookings:
            label = f"❌ {format_date(b[5])} {b[6]}"
            kb.button(text=label, callback_data=f"cancel_select_{b[0]}")
        kb.adjust(1)
        await call.message.answer(
            _multi_booking_text(bookings, "Уточните, какую бронь вы бы хотели отменить?"),
            reply_markup=kb.as_markup(),
        )
    else:
        date_label = f"{format_date(this_booking[5])} {this_booking[6]}"
        kb = InlineKeyboardBuilder()
        kb.button(text="Подтверждаю", callback_data=f"cancel_do_{booking_id}")
        kb.adjust(1)
        await call.message.answer(
            f"Для подтверждения отмены брони на <b>{date_label}</b> нажмите кнопку ниже",
            reply_markup=kb.as_markup(),
            parse_mode="HTML",
        )
    await call.answer()


@router.callback_query(
    F.data.startswith("cancel_select_") & ~F.data.startswith("cancel_select_back")
)
async def cancel_select(call: CallbackQuery):
    booking_id = int(call.data.replace("cancel_select_", ""))
    booking = get_active_booking_by_id(booking_id)
    if not booking:
        await call.message.answer("Бронь не найдена.")
        await call.answer()
        return
    date_label = f"{format_date(booking[5])} {booking[6]}"
    kb = InlineKeyboardBuilder()
    kb.button(text="Подтверждаю", callback_data=f"cancel_do_{booking_id}")
    kb.button(text="◀️ Назад", callback_data="cancel_select_back")
    kb.adjust(1)
    await call.message.answer(
        f"Подтвердите отмену брони на <b>{date_label}</b>",
        reply_markup=kb.as_markup(),
        parse_mode="HTML",
    )
    await call.answer()


@router.callback_query(F.data == "cancel_select_back")
async def cancel_select_back(call: CallbackQuery):
    bookings = get_active_bookings_by_user(call.from_user.id)
    if not bookings:
        await call.message.answer("Активных броней не найдено.")
        await call.answer()
        return
    kb = InlineKeyboardBuilder()
    for b in bookings:
        label = f"❌ {format_date(b[5])} {b[6]}"
        kb.button(text=label, callback_data=f"cancel_select_{b[0]}")
    kb.adjust(1)
    await call.message.answer(
        _multi_booking_text(bookings, "Уточните, какую бронь вы бы хотели отменить?"),
        reply_markup=kb.as_markup(),
    )
    await call.answer()


@router.callback_query(F.data.startswith("cancel_do_"))
async def cancel_do(call: CallbackQuery):
    booking_id = int(call.data.replace("cancel_do_", ""))
    await _remove_ticket_button(booking_id, call.from_user.id)
    await _delete_ticket(booking_id, call.from_user.id)
    update_booking_status(booking_id, "cancelled")
    kb = InlineKeyboardBuilder()
    kb.button(text="Перейти в главное меню", callback_data="main_menu")
    kb.adjust(1)
    await call.message.answer(
        f"Хорошо, спасибо, что предупредили 😊 Ждём Вас на других мероприятиях, "
        f"актуальная афиша всегда на нашем сайте: MoscowStandUpshow.ru\n\n"
        f"При возникновении вопросов - можно писать менеджеру @ccoverr\n\n"
        f"И не забудь заглянуть на наш <a href='{CHANNEL_LINK}'>канал анонсов</a> "
        f"(там часто дарят бесплатные билеты на платные шоу 😉)",
        reply_markup=kb.as_markup(),
        parse_mode="HTML",
    )
    await call.answer()


# ─── ИЗМЕНИТЬ ДАТУ ─────────────────────────────────────────────────────────────

@router.callback_query(F.data.startswith("change_date_do_"))
async def change_date_do(call: CallbackQuery):
    booking_id = int(call.data.replace("change_date_do_", ""))
    await _delete_ticket(booking_id, call.from_user.id)
    update_booking_status(booking_id, "cancelled")
    await call.message.answer("Бронь отменена. Выбери новую дату 👇", reply_markup=await check_dates_kb())
    await call.answer()


@router.callback_query(
    F.data.startswith("change_date_")
    & ~F.data.startswith("change_date_do_")
    & ~F.data.startswith("change_date_select_")
)
async def change_date_confirm(call: CallbackQuery):
    booking_id = call.data.replace("change_date_", "")
    this_booking = await _check_booking_actionable(int(booking_id), call)
    if not this_booking:
        return
    bookings = get_active_bookings_by_user(call.from_user.id)

    if len(bookings) > 1:
        kb = InlineKeyboardBuilder()
        for b in bookings:
            label = f"📅 {format_date(b[5])} {b[6]}"
            kb.button(text=label, callback_data=f"change_date_select_{b[0]}")
        kb.adjust(1)
        await call.message.answer(
            _multi_booking_text(bookings, "Уточните, на какую бронь хотели бы изменить дату?"),
            reply_markup=kb.as_markup(),
        )
    else:
        date_label = f"{format_date(this_booking[5])} {this_booking[6]}"
        kb = InlineKeyboardBuilder()
        kb.button(text="Подтверждаю", callback_data=f"change_date_do_{booking_id}")
        kb.adjust(1)
        await call.message.answer(
            f"Для подтверждения изменения даты брони на <b>{date_label}</b> нажмите кнопку ниже",
            reply_markup=kb.as_markup(),
            parse_mode="HTML",
        )
    await call.answer()


@router.callback_query(
    F.data.startswith("change_date_select_") & ~F.data.startswith("change_date_select_back")
)
async def change_date_select(call: CallbackQuery):
    booking_id = int(call.data.replace("change_date_select_", ""))
    booking = get_active_booking_by_id(booking_id)
    if not booking:
        await call.message.answer("Бронь не найдена.")
        await call.answer()
        return
    date_label = f"{format_date(booking[5])} {booking[6]}"
    kb = InlineKeyboardBuilder()
    kb.button(text="Подтверждаю", callback_data=f"change_date_do_{booking_id}")
    kb.button(text="◀️ Назад", callback_data="change_date_select_back")
    kb.adjust(1)
    await call.message.answer(
        f"Подтвердите изменение даты брони на <b>{date_label}</b>",
        reply_markup=kb.as_markup(),
        parse_mode="HTML",
    )
    await call.answer()


@router.callback_query(F.data == "change_date_select_back")
async def change_date_select_back(call: CallbackQuery):
    bookings = get_active_bookings_by_user(call.from_user.id)
    if not bookings:
        await call.message.answer("Активных броней не найдено.")
        await call.answer()
        return
    kb = InlineKeyboardBuilder()
    for b in bookings:
        label = f"📅 {format_date(b[5])} {b[6]}"
        kb.button(text=label, callback_data=f"change_date_select_{b[0]}")
    kb.adjust(1)
    await call.message.answer(
        _multi_booking_text(bookings, "Уточните, на какую бронь хотели бы изменить дату?"),
        reply_markup=kb.as_markup(),
    )
    await call.answer()


# ─── ИЗМЕНИТЬ КОЛИЧЕСТВО ГОСТЕЙ ────────────────────────────────────────────────

@router.callback_query(F.data.startswith("change_guests_confirm_"))
async def change_guests_confirm(call: CallbackQuery):
    booking_id = call.data.replace("change_guests_confirm_", "")
    this_booking = await _check_booking_actionable(int(booking_id), call)
    if not this_booking:
        return
    bookings = get_active_bookings_by_user(call.from_user.id)

    if len(bookings) > 1:
        kb = InlineKeyboardBuilder()
        for b in bookings:
            label = f"👥 {format_date(b[5])} {b[6]}"
            kb.button(text=label, callback_data=f"change_guests_select_{b[0]}")
        kb.adjust(1)
        await call.message.answer(
            _multi_booking_text(bookings, "Уточните, на какой брони меняем количество гостей?"),
            reply_markup=kb.as_markup(),
        )
    else:
        date_label = f"{format_date(this_booking[5])} {this_booking[6]}"
        kb = InlineKeyboardBuilder()
        kb.button(text="Подтверждаю", callback_data=f"change_guests_do_{booking_id}")
        kb.adjust(1)
        await call.message.answer(
            f"Для подтверждения изменения количества гостей на бронь <b>{date_label}</b> нажмите кнопку ниже 👇",
            reply_markup=kb.as_markup(),
            parse_mode="HTML",
        )
    await call.answer()


@router.callback_query(
    F.data.startswith("change_guests_select_") & ~F.data.startswith("change_guests_select_back")
)
async def change_guests_select(call: CallbackQuery):
    booking_id = int(call.data.replace("change_guests_select_", ""))
    booking = get_active_booking_by_id(booking_id)
    if not booking:
        await call.message.answer("Бронь не найдена.")
        await call.answer()
        return
    date_label = f"{format_date(booking[5])} {booking[6]}"
    kb = InlineKeyboardBuilder()
    kb.button(text="Подтверждаю", callback_data=f"change_guests_do_{booking_id}")
    kb.button(text="◀️ Назад", callback_data="change_guests_select_back")
    kb.adjust(1)
    await call.message.answer(
        f"Подтвердите изменение гостей для брони на <b>{date_label}</b>",
        reply_markup=kb.as_markup(),
        parse_mode="HTML",
    )
    await call.answer()


@router.callback_query(F.data == "change_guests_select_back")
async def change_guests_select_back(call: CallbackQuery):
    bookings = get_active_bookings_by_user(call.from_user.id)
    if not bookings:
        await call.message.answer("Активных броней не найдено.")
        await call.answer()
        return
    kb = InlineKeyboardBuilder()
    for b in bookings:
        label = f"👥 {format_date(b[5])} {b[6]}"
        kb.button(text=label, callback_data=f"change_guests_select_{b[0]}")
    kb.adjust(1)
    await call.message.answer(
        _multi_booking_text(bookings, "Уточните, на какой брони меняем количество гостей?"),
        reply_markup=kb.as_markup(),
    )
    await call.answer()


@router.callback_query(F.data.startswith("change_guests_do_"))
async def change_guests_do(call: CallbackQuery, state: FSMContext):
    booking_id = int(call.data.replace("change_guests_do_", ""))
    booking = get_active_booking_by_id(booking_id)
    if not booking:
        await call.message.answer("Бронь не найдена.")
        await call.answer()
        return
    await state.update_data(booking_id=booking_id)
    await call.message.answer(
        f"{booking[3]}, напишите пожалуйста цифрой, на какое количество человек бронируете?\n\n"
        f"<b>Внимание, бронь на один билет максимум 4 человека</b>",
        parse_mode="HTML",
    )
    await state.set_state(BookingState.waiting_new_guests)
    await call.answer()


@router.message(BookingState.waiting_new_guests)
async def process_new_guests(message: Message, state: FSMContext):
    if not message.text.isdigit():
        await message.answer("Пожалуйста, напишите цифрой количество гостей (от 1 до 4)")
        return
    guests = int(message.text)
    if guests < 1 or guests > 4:
        await message.answer("Максимум 4 человека. Напишите цифру от 1 до 4.")
        return

    data = await state.get_data()
    booking_id = data.get("booking_id")
    booking = get_active_booking_by_id(booking_id)
    if not booking:
        await message.answer("Бронь не найдена.")
        await state.clear()
        return

    event = await get_event(booking[5], booking[6])
    if event:
        total = get_total_guests(booking[5], booking[6], exclude_id=booking_id)
        if total + guests > event["max_seats"]:
            await message.answer(f"К сожалению, доступно только {event['max_seats'] - total} мест. Укажите меньшее количество.")
            return

    update_booking_guests(booking_id, guests)
    date_str = format_date(booking[5])

    kb = InlineKeyboardBuilder()
    kb.button(text="Отменить бронь", callback_data=f"cancel_confirm_{booking_id}")
    kb.button(text="Изменить дату", callback_data=f"change_date_{booking_id}")
    kb.button(text="Изменить количество гостей", callback_data=f"change_guests_confirm_{booking_id}")
    kb.adjust(1)
    await message.answer(
        f"Спасибо, количество гостей изменено на {guests}. Будем ждать Вас {date_str} 👍\n\n"
        f"При возникновении вопросов - можно писать менеджеру @ccoverr (если срочно - звоните {MANAGER_PHONE})",
        reply_markup=kb.as_markup(),
    )
    await state.clear()


@router.message()
async def unknown_message(message: Message, state: FSMContext):
    if await state.get_state() is None:
        from bot.handlers.start import main_menu_kb
        await message.answer("Пожалуйста, выбери вариант из кнопок ниже 👇", reply_markup=main_menu_kb())
