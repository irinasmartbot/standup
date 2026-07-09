from datetime import datetime
from aiogram import Router, F
from aiogram.types import Message, CallbackQuery, FSInputFile, BufferedInputFile
from aiogram.types import ReplyKeyboardMarkup, KeyboardButton, ReplyKeyboardRemove
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.utils.keyboard import InlineKeyboardBuilder

from bot.config import MANAGER_LINK, CHANNEL_LINK, MANAGER_PHONE
from bot.db.crud import (
    get_booking, get_active_booking_by_id, create_booking,
    update_booking_status, update_booking_guests, get_total_guests,
)
from bot.services.sheets import load_events, get_event
from bot.utils.ticket import format_date, guests_word, generate_ticket, MONTHS

router = Router()

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
    kb.adjust(2)
    return kb.as_markup()


async def send_event_card(message, event):
    date_str = format_date(event["date"])
    text = f"{date_str}\n{event['weekday']}\n\n{event['time']}\n{event['address']}\n{event['description']}"
    kb = InlineKeyboardBuilder()
    kb.button(text="🎟 Забронировать билеты", callback_data=f"book_event_{event['date']}_{event['time']}")
    kb.button(text="📋 Правила бронирования", callback_data="booking_rules")
    kb.button(text="◀️ Назад", callback_data="check")
    kb.adjust(1)
    try:
        await message.answer_photo(photo=event["image"], caption=text, reply_markup=kb.as_markup())
    except Exception:
        await message.answer(text, reply_markup=kb.as_markup())


@router.callback_query(lambda c: c.data == "check")
async def check_format(call: CallbackQuery):
    kb = await check_dates_kb()
    await call.message.answer_photo(
        photo=FSInputFile("check_photo.jpg"),
        caption="Выбирай дату 👇",
        reply_markup=kb,
    )
    await call.answer()


@router.callback_query(lambda c: c.data == "by_venue")
async def by_venue(call: CallbackQuery):
    events = await load_events()
    venues = sorted(set(e["location"] for e in events))
    kb = InlineKeyboardBuilder()
    for venue in venues:
        kb.button(text=venue, callback_data=f"venue_{venue}")
    kb.button(text="📅 Выбор по дате", callback_data="check")
    kb.adjust(1)
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
    kb.button(text="◀️ Назад", callback_data="by_venue")
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
            [KeyboardButton(text="✏️ Ввести вручную")],
        ],
        resize_keyboard=True,
        one_time_keyboard=True,
    )


@router.callback_query(lambda c: c.data == "name_confirm")
async def name_confirmed(call: CallbackQuery, state: FSMContext):
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
    if message.text == "✏️ Ввести вручную":
        await message.answer("Напишите ваш номер телефона:", reply_markup=ReplyKeyboardRemove())
        return
    await state.update_data(phone=message.text)
    data = await state.get_data()
    await message.answer(
        f"{data.get('name', '')}, напишите пожалуйста цифрой, на какое количество человек бронируете?\n\n"
        f"<b>Внимание, бронь на один билет максимум 4 человека</b>",
        reply_markup=ReplyKeyboardRemove(),
        parse_mode="HTML",
    )
    await state.set_state(BookingState.waiting_guests)


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
    await message.answer(text, reply_markup=kb.as_markup(), parse_mode="HTML")


@router.callback_query(F.data.startswith("get_ticket_"))
async def get_ticket(call: CallbackQuery):
    booking_id = int(call.data.replace("get_ticket_", ""))
    booking = get_active_booking_by_id(booking_id)
    if not booking:
        await call.message.answer("Бронь не найдена.")
        await call.answer()
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

    kb = InlineKeyboardBuilder()
    kb.button(text="Отменить бронь", callback_data=f"cancel_confirm_{booking_id}")
    kb.button(text="Изменить дату", callback_data=f"change_date_{booking_id}")
    kb.button(text="Изменить количество гостей", callback_data=f"change_guests_confirm_{booking_id}")
    kb.adjust(1)

    await call.message.answer_photo(
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
    await call.message.answer(
        f"Если поменяются планы, пожалуйста, ОБЯЗАТЕЛЬНО НАЖМИТЕ КНОПКУ «Отменить бронь» 😊\n\n"
        f"При возникновении вопросов - можно писать менеджеру @ccoverr (если срочно - звоните {MANAGER_PHONE})\n\n"
        f"И не забудь заглянуть на наш <a href='{CHANNEL_LINK}'>канал анонсов</a> (там часто дарят бесплатные билеты на платные шоу 😉)",
        reply_markup=kb.as_markup(),
        parse_mode="HTML",
    )
    await call.answer()


@router.callback_query(F.data.startswith("cancel_confirm_"))
async def cancel_confirm(call: CallbackQuery):
    booking_id = call.data.replace("cancel_confirm_", "")
    kb = InlineKeyboardBuilder()
    kb.button(text="Подтверждаю", callback_data=f"cancel_do_{booking_id}")
    kb.adjust(1)
    await call.message.answer("Для подтверждения отмены брони нажмите кнопку ниже", reply_markup=kb.as_markup())
    await call.answer()


@router.callback_query(F.data.startswith("cancel_do_"))
async def cancel_do(call: CallbackQuery):
    booking_id = int(call.data.replace("cancel_do_", ""))
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


@router.callback_query(F.data.startswith("change_date_do_"))
async def change_date_do(call: CallbackQuery):
    booking_id = int(call.data.replace("change_date_do_", ""))
    update_booking_status(booking_id, "cancelled")
    await call.message.answer("Бронь отменена. Выбери новую дату 👇", reply_markup=await check_dates_kb())
    await call.answer()


@router.callback_query(F.data.startswith("change_date_") & ~F.data.startswith("change_date_do_"))
async def change_date_confirm(call: CallbackQuery):
    booking_id = call.data.replace("change_date_", "")
    kb = InlineKeyboardBuilder()
    kb.button(text="Подтверждаю", callback_data=f"change_date_do_{booking_id}")
    kb.adjust(1)
    await call.message.answer("Для подтверждения изменения даты нажмите кнопку ниже", reply_markup=kb.as_markup())
    await call.answer()


@router.callback_query(F.data.startswith("change_guests_confirm_"))
async def change_guests_confirm(call: CallbackQuery):
    booking_id = call.data.replace("change_guests_confirm_", "")
    kb = InlineKeyboardBuilder()
    kb.button(text="Подтверждаю", callback_data=f"change_guests_do_{booking_id}")
    kb.adjust(1)
    await call.message.answer(
        "Для подтверждения изменения количества гостей нажмите кнопку ниже 👇",
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
