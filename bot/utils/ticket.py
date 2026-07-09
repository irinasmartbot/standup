from datetime import datetime
from io import BytesIO
from PIL import Image, ImageDraw, ImageFont
from bot.config import TICKET_TEMPLATE

MONTHS = {
    "January": "января", "February": "февраля", "March": "марта",
    "April": "апреля", "May": "мая", "June": "июня",
    "July": "июля", "August": "августа", "September": "сентября",
    "October": "октября", "November": "ноября", "December": "декабря",
}

WEEKDAYS_RU = {
    "Monday": "понедельник", "Tuesday": "вторник", "Wednesday": "среда",
    "Thursday": "четверг", "Friday": "пятница", "Saturday": "суббота", "Sunday": "воскресенье",
}


def format_date(date_str):
    try:
        d = datetime.strptime(date_str, "%d.%m.%Y")
        return d.strftime("%d ") + MONTHS[d.strftime("%B")]
    except Exception:
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


def generate_ticket(name, date_str, time_str, location, guests):
    try:
        img = Image.open(TICKET_TEMPLATE).copy()
    except Exception:
        img = Image.new("RGB", (730, 350), color=(30, 30, 30))

    draw = ImageDraw.Draw(img)

    try:
        font_big = ImageFont.truetype("arial.ttf", 36)
        font_med = ImageFont.truetype("arial.ttf", 28)
        font_small = ImageFont.truetype("arial.ttf", 22)
    except Exception:
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
