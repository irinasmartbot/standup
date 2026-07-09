import asyncio
import aiohttp
import csv
import sqlite3
import os
import logging
from io import StringIO, BytesIO
from datetime import datetime, timedelta
from aiogram import Bot, Dispatcher, F
from aiogram.types import Message, CallbackQuery, FSInputFile, BufferedInputFile, ReplyKeyboardMarkup, KeyboardButton, ReplyKeyboardRemove
from aiogram.filters import CommandStart
from aiogram.utils.keyboard import InlineKeyboardBuilder
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from PIL import Image, ImageDraw, ImageFont

# ===== НАСТРОЙКИ =====
def load_env_file(path=".env"):
    if not os.path.exists(path):
        return
    with open(path, "r", encoding="utf-8") as env_file:
        for line in env_file:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    load_env_file()

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

BOT_TOKEN = os.getenv("BOT_TOKEN")
CSV_URL = os.getenv(
    "CSV_URL",
    "https://docs.google.com/spreadsheets/d/e/2PACX-1vQTZS9GmN4Gkffl6xrUt7W_dDIksHB7z4xAjFDVeR-x4rgWeGJLJPGVfMfY5eQESZcXfBH-ZbrUeMXh/pub?gid=907191184&single=true&output=csv"
)
MANAGER_LINK = os.getenv("MANAGER_LINK", "https://t.me/ccoverr")
CHANNEL_LINK = os.getenv("CHANNEL_LINK", "https://t.me/MoscowStandupShow")
MANAGER_PHONE = os.getenv("MANAGER_PHONE", "89648772410")
DB_PATH = os.getenv("DB_PATH", "bookings.db")
TICKET_TEMPLATE = os.getenv("TICKET_TEMPLATE", "photo_2023-06-26_15-06-46.jpg")

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN is not set. Create .env from .env.example and add Telegram bot token.")

bot = Bot(token=BOT_TOKEN)
storage = MemoryStorage()
dp = Dispatcher(storage=storage)

MONTHS = {
    "January": "января", "February": "февраля", "March": "марта",
    "April": "апреля", "May": "мая", "June": "июня",
    "July": "июля", "August": "августа", "September": "сентября",
    "October": "октября", "November": "ноября", "December": "декабря"
}

WEEKDAYS_RU = {
    "Monday": "понедельник", "Tuesday": "вторник", "Wednesday": "среда",
    "Thursday": "четверг", "Friday": "пятница", "Saturday": "суббота", "Sunday": "воскресенье"
}

# ===== FSM СОСТОЯНИЯ =====
class BookingState(StatesGroup):
    waiting_name = State()
    waiting_phone = State()
    waiting_guests = State()
    waiting_new_guests = State()
    waiting_new_name = State()
    waiting_new_phone = State()

# ===== БАЗА ДАННЫХ =====
def init_db():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("""
        CREATE TABLE IF NOT EXISTS bookings (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            telegram_id INTEGER,
            username TEXT,
            name TEXT,
            phone TEXT,
            event_date TEXT,
            event_time TEXT,
            event_address TEXT,
            event_location TEXT,
            guests INTEGER,
            status TEXT DEFAULT 'booked',
            created_at TEXT
        )
    """)
    c.execute("PRAGMA table_info(bookings)")
    columns = {row[1] for row in c.fetchall()}
    migrations = {
        "reminder_24h_sent": "ALTER TABLE bookings ADD COLUMN reminder_24h_sent INTEGER DEFAULT 0",
        "reminder_day_sent": "ALTER TABLE bookings ADD COLUMN reminder_day_sent INTEGER DEFAULT 0",
        "annulled_at": "ALTER TABLE bookings ADD COLUMN annulled_at TEXT",
    }
    for column, sql in migrations.items():
        if column not in columns:
            c.execute(sql)
    conn.commit()
    conn.close()

def get_booking(telegram_id, event_date, event_time):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT * FROM bookings WHERE telegram_id=? AND event_date=? AND event_time=? AND status IN ('booked', 'confirmed')",
              (telegram_id, event_date, event_time))
    row = c.fetchone()
    conn.close()
    return row

def get_active_booking_by_id(booking_id):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT * FROM bookings WHERE id=? AND status IN ('booked', 'confirmed')", (booking_id,))
    row = c.fetchone()
    conn.close()
    return row

def create_booking(telegram_id, username, name, phone, event_date, event_time, event_address, event_location, guests):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("""
        INSERT INTO bookings (telegram_id, username, name, phone, event_date, event_time, event_address, event_location, guests, status, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'booked', ?)
    """, (telegram_id, username, name, phone, event_date, event_time, event_address, event_location, guests, datetime.now().isoformat()))
    booking_id = c.lastrowid
    conn.commit()
    conn.close()
    return booking_id

def update_booking_status(booking_id, status):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("UPDATE bookings SET status=? WHERE id=?", (status, booking_id))
    conn.commit()
    conn.close()

def update_booking_guests(booking_id, guests):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("UPDATE bookings SET guests=? WHERE id=?", (guests, booking_id))
    conn.commit()
    conn.close()

def get_total_guests(event_date, event_time, exclude_id=None):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    if exclude_id:
        c.execute("SELECT SUM(guests) FROM bookings WHERE event_date=? AND event_time=? AND status IN ('booked', 'confirmed') AND id!=?",
                  (event_date, event_time, exclude_id))
    else:
        c.execute("SELECT SUM(guests) FROM bookings WHERE event_date=? AND event_time=? AND status IN ('booked', 'confirmed')",
                  (event_date, event_time))
    result = c.fetchone()[0]
    conn.close()
    return result or 0

def update_reminder_flag(booking_id, flag):
    if flag not in {"reminder_24h_sent", "reminder_day_sent"}:
        return
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute(f"UPDATE bookings SET {flag}=1 WHERE id=?", (booking_id,))
    conn.commit()
    conn.close()

def annul_booking(booking_id):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute(
        "UPDATE bookings SET status='annulled', annulled_at=? WHERE id=? AND status='booked'",
        (datetime.now().isoformat(), booking_id)
    )
    conn.commit()
    conn.close()

def get_booked_for_reminders():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("""
        SELECT id, telegram_id, name, event_date, event_time, event_address, event_location,
               guests, created_at, reminder_24h_sent, reminder_day_sent
        FROM bookings
        WHERE status='booked'
    """)
    rows = c.fetchall()
    conn.close()
    return rows

# ===== ЗАГРУЗКА ТАБЛИЦЫ =====
async def load_events():
    async with aiohttp.ClientSession() as session:
        async with session.get(CSV_URL) as resp:
            text = await resp.text(encoding="utf-8-sig")
    reader = csv.reader(StringIO(text))
    rows = list(reader)
    events = []
    for row in rows[1:]:
        if len(row) < 17:
            continue
        status = row[16].strip()
        if status != "Актуально":
            continue
        try:
            date = datetime.strptime(row[1].strip(), "%d.%m.%Y")
        except:
            continue
        if date.date() < datetime.now().date():
            continue
        # Лимит мест
        try:
            extra = int(row[9].strip()) if row[9].strip() else 0
        except:
            extra = 0
        max_seats = 60 + abs(extra)

        events.append({
            "date": row[1].strip(),
            "weekday": row[2].strip(),
            "time": row[3].strip(),
            "address": row[4].strip(),
            "description": row[5].strip(),
            "image": row[6].strip(),
            "location": row[10].strip(),
            "max_seats": max_seats,
        })
    return events

async def get_event(event_date, event_time):
    events = await load_events()
    return next((e for e in events if e["date"] == event_date and e["time"] == event_time), None)

def format_date(date_str):
    try:
        d = datetime.strptime(date_str, "%d.%m.%Y")
        return d.strftime("%d ") + MONTHS[d.strftime("%B")]
    except:
        return date_str

def parse_event_datetime(date_str, time_str):
    clean_time = (time_str or "").strip().replace(".", ":")
    for fmt in ("%d.%m.%Y %H:%M", "%d.%m.%Y %H"):
        try:
            return datetime.strptime(f"{date_str} {clean_time}", fmt)
        except ValueError:
            continue
    return None

def parse_created_at(value):
    try:
        return datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return datetime.now()

def guests_word(n):
    if n == 1:
        return "1 гость"
    elif 2 <= n <= 4:
        return f"{n} гостя"
    else:
        return f"{n} гостей"

# ===== ГЕНЕРАЦИЯ БИЛЕТА =====
def generate_ticket(name, date_str, time_str, location, guests):
    try:
        img = Image.open(TICKET_TEMPLATE).copy()
    except:
        img = Image.new("RGB", (730, 350), color=(30, 30, 30))

    draw = ImageDraw.Draw(img)

    try:
        font_big = ImageFont.truetype("arial.ttf", 36)
        font_med = ImageFont.truetype("arial.ttf", 28)
        font_small = ImageFont.truetype("arial.ttf", 22)
    except:
        font_big = ImageFont.load_default()
        font_med = font_big
        font_small = font_big

    x = 30
    draw.text((x, 80), name, font=font_big, fill="white")
    draw.text((x, 130), f"{date_str}        {time_str}", font=font_med, fill="white")
    draw.text((x, 175), location, font=font_small, fill="white")
    draw.text((x, 220), guests_word(guests), font=font_med, fill="white")

    buf = BytesIO()
    img.save(buf, format="JPEG")
    buf.seek(0)
    return buf

# ===== ГЛАВНОЕ МЕНЮ =====
WELCOME_TEXT = """Привет! Это Moscow StandUp Show! Мы делаем шоу в различных заведениях в центре Москвы каждый день!

Только опытные комики, участники проектов ТНТ и YouTube, харизматичные ведущие, интерактив со зрителями, атмосферные залы, подарки на каждом мероприятии - это всё мы! 😊

Здесь ты сможешь узнать о нас побольше и забронировать места:"""

def main_menu_kb():
    kb = InlineKeyboardBuilder()
    kb.button(text="🎟 Забронировать места", callback_data="book")
    kb.button(text="🎭 Наши форматы ШОУ", callback_data="formats")
    kb.button(text="📍 Наши площадки", callback_data="venues")
    kb.button(text="📋 Правила посещения шоу", callback_data="rules")
    kb.button(text="💬 Задать вопрос менеджеру", url=MANAGER_LINK)
    kb.button(text="📢 Заглянуть на наш канал анонсов", url=CHANNEL_LINK)
    kb.adjust(1)
    return kb.as_markup()

@dp.message(CommandStart())
async def start(message: Message, state: FSMContext):
    await state.clear()
    await message.answer(WELCOME_TEXT, reply_markup=main_menu_kb())

@dp.callback_query(F.data == "main_menu")
async def back_to_menu(call: CallbackQuery, state: FSMContext):
    await state.clear()
    await call.message.answer(WELCOME_TEXT, reply_markup=main_menu_kb())
    await call.answer()

# ===== НАШИ ФОРМАТЫ ШОУ =====
FORMATS_TEXT = """🎭 <b>Наши форматы шоу:</b>

<b>Формат StandUp BEST:</b>
Только лучший, уже проверенный стэндап материал от троих комиков, именитых участников многочисленных телевизионных проектов. Вы не услышите ни одной несмешной шутки, только BEST!!
Билеты - от 500 рублей.

<b>Формат StandUp Проверка материала:</b>
5-7 опытных комиков, участников известных проектов ТНТ и YouTube, рассказывают по 10-15 минут свежих, но не проверенных шуток. Вы услышите настоящий эксклюзив и поможете комикам понять, что смешно, а что стоит убрать из материала 🐒
Вход бесплатный."""

@dp.callback_query(F.data == "formats")
async def show_formats(call: CallbackQuery):
    kb = InlineKeyboardBuilder()
    kb.button(text="🎟 Бронь Формат StandUp BEST", callback_data="best")
    kb.button(text="🎟 Бронь Формат StandUp Проверка материала", callback_data="check")
    kb.button(text="◀️ Назад в меню", callback_data="main_menu")
    kb.adjust(1)
    await call.message.answer(FORMATS_TEXT, reply_markup=kb.as_markup(), parse_mode="HTML")
    await call.answer()

# ===== НАШИ ПЛОЩАДКИ =====
@dp.callback_query(F.data == "venues")
async def show_venues(call: CallbackQuery):
    kb = InlineKeyboardBuilder()
    kb.button(text="🎟 Забронировать места", callback_data="book")
    kb.button(text="🎭 Наши форматы ШОУ", callback_data="formats")
    kb.button(text="📋 Правила посещения шоу", callback_data="rules")
    kb.button(text="💬 Задать вопрос менеджеру", url=MANAGER_LINK)
    kb.button(text="📢 Заглянуть на наш канал анонсов", url=CHANNEL_LINK)
    kb.adjust(1)

    venues_info = [
        ("temple_bar.jpg", "<b>Temple Bar</b> — это английская респектабельность, ирландское жизнелюбие и русское гостеприимство в одном ресторане, где каждый гость будет чувствовать демократическую атмосферу, и сможет насладиться великолепными стейками, большим ассортиментом коктейлей, а также отменными блюдами из мяса и овощей на мангале."),
        ("escobar.jpg", "<b>Escobar</b> — бар с неординарной кухней, расположенный в комплексе исторических зданий 18-19 веков, брутальный дизайн в эстетике фильмов Квентина Тарантино, с легким оттенком латиноамериканской расслабленности."),
        ("nebar.jpg", "<b>Небар</b> — один из самых популярных и громких баров столицы с уникальным стилем. Авторская коктейльная карта для тех, кто любит эксперименты, насчитывает 13 сезонных коктейлей на любой вкус, названных в честь известных городов мира."),
    ]

    await call.message.answer("📍 <b>Наши площадки:</b>\n\nМероприятия проходят в заведениях, где каждый найдёт что-то на свой вкус!", parse_mode="HTML")

    for i, (photo_file, caption) in enumerate(venues_info):
        is_last = i == len(venues_info) - 1
        try:
            await call.message.answer_photo(
                photo=FSInputFile(photo_file),
                caption=caption,
                parse_mode="HTML",
                reply_markup=kb.as_markup() if is_last else None
            )
        except:
            await call.message.answer(caption, parse_mode="HTML", reply_markup=kb.as_markup() if is_last else None)
    await call.answer()

# ===== ПРАВИЛА ПОСЕЩЕНИЯ =====
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

@dp.callback_query(F.data == "rules")
async def show_rules(call: CallbackQuery):
    kb = InlineKeyboardBuilder()
    kb.button(text="🎟 Забронировать места", callback_data="book")
    kb.button(text="🎭 Наши форматы ШОУ", callback_data="formats")
    kb.button(text="📍 Наши площадки", callback_data="venues")
    kb.button(text="💬 Задать вопрос менеджеру", url=MANAGER_LINK)
    kb.button(text="📢 Заглянуть на наш канал анонсов", url=CHANNEL_LINK)
    kb.adjust(1)
    await call.message.answer(RULES_TEXT, reply_markup=kb.as_markup(), parse_mode="HTML")
    await call.answer()

# ===== ВЫБОР ФОРМАТА =====
@dp.callback_query(F.data == "book")
async def book(call: CallbackQuery):
    kb = InlineKeyboardBuilder()
    kb.button(text="STANDUP BEST", callback_data="best")
    kb.button(text="StandUp Проверка материала", callback_data="check")
    kb.adjust(1)
    await call.message.answer(
        "Привет! 😊 Я помогу тебе забронировать места на мероприятия от Moscow StandUp Show 🎤\n\nВыбирай формат шоу 👇",
        reply_markup=kb.as_markup()
    )
    await call.answer()

@dp.callback_query(F.data == "best")
async def best_format(call: CallbackQuery):
    kb = InlineKeyboardBuilder()
    kb.button(text="💬 Задать вопрос менеджеру", url=MANAGER_LINK)
    kb.button(text="◀️ Назад в меню", callback_data="main_menu")
    kb.adjust(1)
    await call.message.answer(
        "Формат <b>StandUp BEST</b> — платные шоу с билетами от 500 ₽.\n\n"
        "Бронирование через бот для этого формата скоро появится. "
        "Сейчас можно забронировать через менеджера 👇",
        reply_markup=kb.as_markup(),
        parse_mode="HTML",
    )
    await call.answer()

# ===== ВЫБОР ДАТЫ =====
async def check_dates_kb():
    events = await load_events()
    dates = sorted(set(e["date"] for e in events))
    kb = InlineKeyboardBuilder()
    for date in dates:
        try:
            d = datetime.strptime(date, "%d.%m.%Y")
            label = d.strftime("%d ") + MONTHS[d.strftime("%B")]
        except:
            label = date
        kb.button(text=label, callback_data=f"date_{date}")
    kb.button(text="📍 Выбор по площадке", callback_data="by_venue")
    kb.adjust(2)
    return kb.as_markup()

@dp.callback_query(F.data == "check")
async def check_format(call: CallbackQuery):
    kb = await check_dates_kb()
    await call.message.answer_photo(
        photo=FSInputFile("check_photo.jpg"),
        caption="Выбирай дату 👇",
        reply_markup=kb
    )
    await call.answer()

# ===== ВЫБОР ПО ПЛОЩАДКЕ =====
@dp.callback_query(F.data == "by_venue")
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

@dp.callback_query(F.data.startswith("venue_"))
async def venue_events(call: CallbackQuery):
    venue = call.data.replace("venue_", "")
    events = await load_events()
    filtered = [e for e in events if e["location"] == venue]
    filtered.sort(key=lambda x: datetime.strptime(x["date"], "%d.%m.%Y"))
    kb = InlineKeyboardBuilder()
    for e in filtered:
        try:
            d = datetime.strptime(e["date"], "%d.%m.%Y")
            label = f"📅 {d.strftime('%d ')+MONTHS[d.strftime('%B')]} ({e['weekday']}) {e['time']}"
        except:
            label = e["date"]
        kb.button(text=label, callback_data=f"event_{e['date']}_{e['time']}")
    kb.button(text="◀️ Назад", callback_data="by_venue")
    kb.adjust(1)
    await call.message.answer(f"Мероприятия в {venue} 👇", reply_markup=kb.as_markup())
    await call.answer()

# ===== КАРТОЧКА МЕРОПРИЯТИЯ =====
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
    except:
        await message.answer(text, reply_markup=kb.as_markup())

@dp.callback_query(F.data.startswith("date_"))
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

@dp.callback_query(F.data.startswith("event_"))
async def show_specific_event(call: CallbackQuery):
    event_date, event_time = call.data.replace("event_", "", 1).split("_", 1)
    events = await load_events()
    event = next((e for e in events if e["date"] == event_date and e["time"] == event_time), None)
    if event:
        await send_event_card(call.message, event)
    await call.answer()

# ===== ПРАВИЛА БРОНИРОВАНИЯ =====
BOOKING_RULES_TEXT = """📋 <b>Порядок посещения шоу:</b>

1. Сбор гостей начинается за полчаса до начала шоу

2. Рассадка осуществляется по мере прихода, чтобы занять лучшие места, приходите вовремя 👊 Возможна подсадка за один стол других гостей для небольших компаний.

3. Обратите внимание, что при посещении шоу заказ минимум одной позиции по меню является обязательным.

4. Если поменяются планы, пожалуйста, ОБЯЗАТЕЛЬНО ПРЕДУПРЕДИТЕ 😊 - у вас будет возможность отменить бронь или изменить дату при необходимости 👌"""

@dp.callback_query(F.data == "booking_rules")
async def show_booking_rules(call: CallbackQuery):
    await call.message.answer(BOOKING_RULES_TEXT, parse_mode="HTML")
    await call.answer()

# ===== ВОРОНКА БРОНИРОВАНИЯ =====
@dp.callback_query(F.data.startswith("book_event_"))
async def start_booking(call: CallbackQuery, state: FSMContext):
    event_date, event_time = call.data.replace("book_event_", "", 1).split("_", 1)

    # Проверяем что мероприятие не прошло
    try:
        event_dt = datetime.strptime(event_date, "%d.%m.%Y")
        if event_dt.date() < datetime.now().date():
            await call.message.answer("Это мероприятие уже прошло 😊 Выбери новую дату!", reply_markup=await check_dates_kb())
            await call.answer()
            return
    except:
        pass

    # Проверяем повторную бронь
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
            reply_markup=kb.as_markup()
        )
        await call.answer()
        return

    # Сохраняем данные мероприятия
    await state.update_data(event_date=event_date, event_time=event_time)

    # Получаем имя из профиля
    name = call.from_user.first_name or ""
    if call.from_user.last_name:
        name += f" {call.from_user.last_name}"

    kb = InlineKeyboardBuilder()
    kb.button(text="Все верно 👌", callback_data="name_confirm")
    kb.button(text="Изменить", callback_data="name_change")
    kb.adjust(2)

    await state.update_data(name=name)
    await call.message.answer(
        f"Для бронирования вам нужно заполнить некоторые данные\n\nВаше имя <b>{name}</b>, верно?",
        reply_markup=kb.as_markup(),
        parse_mode="HTML"
    )
    await call.answer()

@dp.callback_query(F.data == "name_confirm")
async def name_confirmed(call: CallbackQuery, state: FSMContext):
    phone_kb = ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="📱 Поделиться номером", request_contact=True)],
            [KeyboardButton(text="✏️ Ввести вручную")]
        ],
        resize_keyboard=True,
        one_time_keyboard=True
    )
    await call.message.answer(
        "Поделитесь номером телефона или введите вручную:",
        reply_markup=phone_kb
    )
    await state.set_state(BookingState.waiting_phone)
    await call.answer()

@dp.callback_query(F.data == "name_change")
async def name_change(call: CallbackQuery, state: FSMContext):
    await call.message.answer("Напишите, пожалуйста, ваше имя.")
    await state.set_state(BookingState.waiting_name)
    await call.answer()

@dp.message(BookingState.waiting_name)
async def process_name(message: Message, state: FSMContext):
    await state.update_data(name=message.text)
    phone_kb = ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="📱 Поделиться номером", request_contact=True)],
            [KeyboardButton(text="✏️ Ввести вручную")]
        ],
        resize_keyboard=True,
        one_time_keyboard=True
    )
    await message.answer(
        "Поделитесь номером телефона или введите вручную:",
        reply_markup=phone_kb
    )
    await state.set_state(BookingState.waiting_phone)

# ШАГ 2 — ТЕЛЕФОН
@dp.message(BookingState.waiting_phone, F.contact)
async def process_phone_contact(message: Message, state: FSMContext):
    phone = message.contact.phone_number
    await state.update_data(phone=phone)
    data = await state.get_data()
    name = data.get("name", "")
    await message.answer(
        f"{name}, напишите пожалуйста цифрой, на какое количество человек бронируете?\n\n"
        f"<b>Внимание, бронь на один билет максимум 4 человека</b>",
        reply_markup=ReplyKeyboardRemove(),
        parse_mode="HTML"
    )
    await state.set_state(BookingState.waiting_guests)

@dp.message(BookingState.waiting_phone)
async def process_phone_text(message: Message, state: FSMContext):
    if message.text == "✏️ Ввести вручную":
        await message.answer("Напишите ваш номер телефона:", reply_markup=ReplyKeyboardRemove())
        return
    await state.update_data(phone=message.text)
    data = await state.get_data()
    name = data.get("name", "")
    await message.answer(
        f"{name}, напишите пожалуйста цифрой, на какое количество человек бронируете?\n\n"
        f"<b>Внимание, бронь на один билет максимум 4 человека</b>",
        reply_markup=ReplyKeyboardRemove(),
        parse_mode="HTML"
    )
    await state.set_state(BookingState.waiting_guests)

@dp.callback_query(F.data == "phone_change")
async def phone_change(call: CallbackQuery, state: FSMContext):
    phone_kb = ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="📱 Поделиться номером", request_contact=True)],
            [KeyboardButton(text="✏️ Ввести вручную")]
        ],
        resize_keyboard=True,
        one_time_keyboard=True
    )
    await call.message.answer("Поделитесь номером телефона или введите вручную:", reply_markup=phone_kb)
    await state.set_state(BookingState.waiting_phone)
    await call.answer()

# ШАГ 3 — ГОСТИ
@dp.message(BookingState.waiting_guests)
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

    # Проверяем лимит мест
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

    # Создаём бронь
    event_address = event["address"] if event else ""
    event_location = event["location"] if event else ""
    booking_id = create_booking(
        message.from_user.id,
        message.from_user.username or "",
        name, phone, event_date, event_time,
        event_address, event_location, guests
    )

    date_str = format_date(event_date)
    await state.update_data(booking_id=booking_id)
    await state.set_state(None)

    # Проверяем нужно ли сразу показать кнопку получить билет
    now = datetime.now()
    try:
        event_dt = datetime.strptime(event_date, "%d.%m.%Y")
        days_until = (event_dt.date() - now.date()).days
    except:
        days_until = 99

    kb = InlineKeyboardBuilder()

    if days_until <= 1:
        # Бронь в день шоу или за день — сразу показываем кнопку билета
        kb.button(text="🎟 Получить билет 🎟", callback_data=f"get_ticket_{booking_id}")
        kb.button(text="Отменить бронь", callback_data=f"cancel_confirm_{booking_id}")
        kb.button(text="Изменить дату", callback_data=f"change_date_{booking_id}")
        kb.button(text="Изменить количество гостей", callback_data=f"change_guests_confirm_{booking_id}")
        kb.button(text="💬 Задать вопрос менеджеру", url=MANAGER_LINK)
        kb.button(text="📢 Заглянуть на наш канал анонсов", url=CHANNEL_LINK)
        kb.adjust(1)
        await message.answer(
            f"Отлично! Мы внесли Вас в списки гостей:\n\n"
            f"<b>Дата:</b> {date_str} ({event['weekday'] if event else ''})\n"
            f"<b>Время:</b> {event_time}\n"
            f"<b>Локация:</b> {event_address}\n"
            f"<b>Количество гостей:</b> {guests} чел.\n\n"
            f"<b>❗ Важная информация — для того чтобы мы окончательно закрепили за Вами место ОБЯЗАТЕЛЬНО подтвердите бронь, нажав на кнопку «Получить билет»</b>\n\n"
            f"<b>❗ Внимание, если Вы не успеете подтвердить бронь, она будет аннулирована.</b>",
            reply_markup=kb.as_markup(),
            parse_mode="HTML"
        )
    else:
        kb.button(text="Отменить бронь", callback_data=f"cancel_confirm_{booking_id}")
        kb.button(text="Изменить дату", callback_data=f"change_date_{booking_id}")
        kb.button(text="Изменить количество гостей", callback_data=f"change_guests_confirm_{booking_id}")
        kb.button(text="💬 Задать вопрос менеджеру", url=MANAGER_LINK)
        kb.button(text="📢 Заглянуть на наш канал анонсов", url=CHANNEL_LINK)
        kb.adjust(1)
        await message.answer(
            f"Отлично! Мы внесли Вас в списки гостей:\n\n"
            f"<b>Дата:</b> {date_str} ({event['weekday'] if event else ''})\n"
            f"<b>Время:</b> {event_time}\n"
            f"<b>Локация:</b> {event_address}\n"
            f"<b>Количество гостей:</b> {guests} чел.\n\n"
            f"<b>❗ Внимание, за сутки до мероприятия Вам придёт сообщение-напоминание с подробностями и кнопкой «Получить билет». Обязательно нажмите кнопку, чтобы подтвердить бронь. Если Вы не успеете подтвердить бронь, она будет аннулирована.</b>\n\n"
            f"Если поменяются планы, обязательно предупредите 😊",
            reply_markup=kb.as_markup(),
            parse_mode="HTML"
        )

# ===== ПОЛУЧИТЬ БИЛЕТ =====
@dp.callback_query(F.data.startswith("get_ticket_"))
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

    # Генерируем билет
    ticket_buf = generate_ticket(name, event_date, event_time, short_address, guests)

    # Обновляем статус
    update_booking_status(booking_id, "confirmed")

    kb = InlineKeyboardBuilder()
    kb.button(text="Отменить бронь", callback_data=f"cancel_confirm_{booking_id}")
    kb.button(text="Изменить дату", callback_data=f"change_date_{booking_id}")
    kb.button(text="Изменить количество гостей", callback_data=f"change_guests_confirm_{booking_id}")
    kb.adjust(1)

    await call.message.answer_photo(
        photo=BufferedInputFile(ticket_buf.getvalue(), filename=f"ticket_{booking_id}.jpg"),
        caption=f"Отлично!\n\nДанные по билету:\n\n"
                f"Ваше имя: {name}\n"
                f"Дата: {event_date}\n"
                f"Время: {event_time}\n"
                f"Место: {event_address}\n"
                f"Количество гостей: {guests_word(guests)}\n\n"
                f"Ждем вас на мероприятии ❤️"
    )

    await call.message.answer(
        f"Если поменяются планы, пожалуйста, ОБЯЗАТЕЛЬНО НАЖМИТЕ КНОПКУ «Отменить бронь» 😊\n\n"
        f"При возникновении вопросов - можно писать менеджеру @ccoverr (если срочно - звоните {MANAGER_PHONE})\n\n"
        f"И не забудь заглянуть на наш <a href='{CHANNEL_LINK}'>канал анонсов</a> (там часто дарят бесплатные билеты на платные шоу 😉)",
        reply_markup=kb.as_markup(),
        parse_mode="HTML"
    )
    await call.answer()

# ===== ОТМЕНА БРОНИ =====
@dp.callback_query(F.data.startswith("cancel_confirm_"))
async def cancel_confirm(call: CallbackQuery):
    booking_id = call.data.replace("cancel_confirm_", "")
    kb = InlineKeyboardBuilder()
    kb.button(text="Подтверждаю", callback_data=f"cancel_do_{booking_id}")
    kb.adjust(1)
    await call.message.answer(
        "Для подтверждения отмены брони нажмите кнопку ниже",
        reply_markup=kb.as_markup()
    )
    await call.answer()

@dp.callback_query(F.data.startswith("cancel_do_"))
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
        parse_mode="HTML"
    )
    await call.answer()

# ===== ИЗМЕНИТЬ ДАТУ =====
@dp.callback_query(F.data.startswith("change_date_do_"))
async def change_date_do(call: CallbackQuery):
    booking_id = int(call.data.replace("change_date_do_", ""))
    update_booking_status(booking_id, "cancelled")
    kb = await check_dates_kb()
    await call.message.answer(
        "Бронь отменена. Выбери новую дату 👇",
        reply_markup=kb
    )
    await call.answer()

@dp.callback_query(F.data.startswith("change_date_") & ~F.data.startswith("change_date_do_"))
async def change_date_confirm(call: CallbackQuery):
    booking_id = call.data.replace("change_date_", "")
    kb = InlineKeyboardBuilder()
    kb.button(text="Подтверждаю", callback_data=f"change_date_do_{booking_id}")
    kb.adjust(1)
    await call.message.answer(
        "Для подтверждения изменения даты нажмите кнопку ниже",
        reply_markup=kb.as_markup()
    )
    await call.answer()

# ===== ИЗМЕНИТЬ КОЛИЧЕСТВО ГОСТЕЙ =====
@dp.callback_query(F.data.startswith("change_guests_confirm_"))
async def change_guests_confirm(call: CallbackQuery):
    booking_id = call.data.replace("change_guests_confirm_", "")
    kb = InlineKeyboardBuilder()
    kb.button(text="Подтверждаю", callback_data=f"change_guests_do_{booking_id}")
    kb.adjust(1)
    await call.message.answer(
        "Для подтверждения изменения количества гостей нажмите кнопку ниже 👇",
        reply_markup=kb.as_markup()
    )
    await call.answer()

@dp.callback_query(F.data.startswith("change_guests_do_"))
async def change_guests_do(call: CallbackQuery, state: FSMContext):
    booking_id = int(call.data.replace("change_guests_do_", ""))
    booking = get_active_booking_by_id(booking_id)
    if not booking:
        await call.message.answer("Бронь не найдена.")
        await call.answer()
        return
    name = booking[3]
    await state.update_data(booking_id=booking_id)
    await call.message.answer(
        f"{name}, напишите пожалуйста цифрой, на какое количество человек бронируете?\n\n"
        f"<b>Внимание, бронь на один билет максимум 4 человека</b>",
        parse_mode="HTML"
    )
    await state.set_state(BookingState.waiting_new_guests)
    await call.answer()

@dp.message(BookingState.waiting_new_guests)
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

    event_date = booking[5]
    event_time = booking[6]

    # Проверяем лимит
    event = await get_event(event_date, event_time)
    if event:
        total = get_total_guests(event_date, event_time, exclude_id=booking_id)
        if total + guests > event["max_seats"]:
            available = event["max_seats"] - total
            await message.answer(f"К сожалению, доступно только {available} мест. Укажите меньшее количество.")
            return

    update_booking_guests(booking_id, guests)
    date_str = format_date(event_date)

    kb = InlineKeyboardBuilder()
    kb.button(text="Отменить бронь", callback_data=f"cancel_confirm_{booking_id}")
    kb.button(text="Изменить дату", callback_data=f"change_date_{booking_id}")
    kb.button(text="Изменить количество гостей", callback_data=f"change_guests_confirm_{booking_id}")
    kb.adjust(1)

    await message.answer(
        f"Спасибо, количество гостей изменено на {guests}. Будем ждать Вас {date_str} 👍\n\n"
        f"При возникновении вопросов - можно писать менеджеру @ccoverr (если срочно - звоните {MANAGER_PHONE})",
        reply_markup=kb.as_markup()
    )
    await state.clear()

# ===== НАПОМИНАНИЯ И АННУЛИРОВАНИЕ =====
def booking_manage_kb(booking_id, include_ticket=True):
    kb = InlineKeyboardBuilder()
    if include_ticket:
        kb.button(text="🎟 Получить билет", callback_data=f"get_ticket_{booking_id}")
    kb.button(text="Отменить бронь", callback_data=f"cancel_confirm_{booking_id}")
    kb.button(text="Изменить дату", callback_data=f"change_date_{booking_id}")
    kb.button(text="Изменить количество гостей", callback_data=f"change_guests_confirm_{booking_id}")
    kb.adjust(1)
    return kb.as_markup()

async def send_booking_reminder(row, reminder_type):
    booking_id, telegram_id, name, event_date, event_time, event_address, event_location, guests, *_ = row
    date_str = format_date(event_date)

    if reminder_type == "day":
        text = (
            f"{name}, мне необходимо подтвердить, либо отменить Вашу бронь на {date_str} в {event_time} 😊\n\n"
            f"Чтобы подтвердить бронь, нажми на «Получить билет» 👇"
        )
    else:
        text = (
            f"Напоминание о брони на Moscow StandUp Show:\n\n"
            f"<b>Дата:</b> {date_str}\n"
            f"<b>Время:</b> {event_time}\n"
            f"<b>Адрес:</b> {event_address}\n"
            f"<b>Количество гостей:</b> {guests} чел.\n\n"
            f"Сбор гостей начинается за полчаса до начала шоу. "
            f"Для подтверждения брони нажмите «Получить билет» 👇"
        )

    await bot.send_message(
        telegram_id,
        text,
        reply_markup=booking_manage_kb(booking_id),
        parse_mode="HTML"
    )

async def send_annulled_message(row):
    booking_id, telegram_id, *_ = row
    kb = InlineKeyboardBuilder()
    kb.button(text="Перейти в главное меню", callback_data="main_menu")
    kb.adjust(1)
    await bot.send_message(
        telegram_id,
        f"Ваша бронь аннулирована, ждём Вас на других мероприятиях 😊\n\n"
        f"При возникновении вопросов - можно писать менеджеру @ccoverr\n\n"
        f"И не забудь заглянуть на наш <a href='{CHANNEL_LINK}'>канал анонсов</a>",
        reply_markup=kb.as_markup(),
        parse_mode="HTML"
    )
    annul_booking(booking_id)

async def process_due_reminders():
    now = datetime.now()
    for row in get_booked_for_reminders():
        booking_id = row[0]
        event_date = row[3]
        event_time = row[4]
        created_at = parse_created_at(row[8])
        reminder_24h_sent = bool(row[9])
        reminder_day_sent = bool(row[10])
        event_dt = parse_event_datetime(event_date, event_time)
        if not event_dt:
            logger.warning("Cannot parse event datetime for booking %s: %s %s", booking_id, event_date, event_time)
            continue

        one_day_reminder_at = datetime.combine(event_dt.date() - timedelta(days=1), datetime.min.time()).replace(hour=14)
        day_reminder_at = datetime.combine(event_dt.date(), datetime.min.time()).replace(hour=10)
        same_day_first_reminder_at = created_at + (timedelta(minutes=20) if event_dt - created_at <= timedelta(hours=1) else timedelta(hours=1))

        try:
            if event_dt.date() == now.date() and not reminder_24h_sent and now >= same_day_first_reminder_at:
                await send_booking_reminder(row, "24h")
                update_reminder_flag(booking_id, "reminder_24h_sent")
                reminder_24h_sent = True
            elif not reminder_24h_sent and now >= one_day_reminder_at and now < event_dt:
                await send_booking_reminder(row, "24h")
                update_reminder_flag(booking_id, "reminder_24h_sent")
                reminder_24h_sent = True

            if not reminder_day_sent and now >= day_reminder_at and now < event_dt:
                await send_booking_reminder(row, "day")
                update_reminder_flag(booking_id, "reminder_day_sent")
                reminder_day_sent = True

            if created_at <= event_dt - timedelta(hours=3):
                annul_at = event_dt - timedelta(hours=2)
            else:
                last_reminder_at = same_day_first_reminder_at if event_dt.date() == created_at.date() else max(one_day_reminder_at, day_reminder_at)
                annul_at = last_reminder_at + timedelta(minutes=30)

            if now >= annul_at and now < event_dt:
                await send_annulled_message(row)
        except Exception:
            logger.exception("Failed to process reminder for booking %s", booking_id)

async def reminder_loop():
    while True:
        await process_due_reminders()
        await asyncio.sleep(60)

# ===== ЗАЩИТА ОТ СЛУЧАЙНОГО ТЕКСТА =====
@dp.message()
async def unknown_message(message: Message, state: FSMContext):
    current_state = await state.get_state()
    if current_state is None:
        await message.answer("Пожалуйста, выбери вариант из кнопок ниже 👇", reply_markup=main_menu_kb())

# ===== ЗАПУСК =====
async def main():
    init_db()
    asyncio.create_task(reminder_loop())
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
