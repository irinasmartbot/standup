from datetime import datetime, timedelta, timezone
from io import BytesIO

from PIL import Image, ImageDraw, ImageFont

from bot.config import TICKET_TEMPLATE

# Москва / GMT+3 (без DST)
MSK = timezone(timedelta(hours=3))

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


def now_msk() -> datetime:
    return datetime.now(MSK)


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
        parsed = datetime.fromisoformat(value)
        if parsed.tzinfo is not None:
            return parsed.astimezone(MSK).replace(tzinfo=None)
        return parsed
    except (TypeError, ValueError):
        return now_msk().replace(tzinfo=None)


def guests_word(n):
    if n == 1:
        return "1 гость"
    elif 2 <= n <= 4:
        return f"{n} гостя"
    else:
        return f"{n} гостей"


def generate_ticket(name, date_str, time_str, location, guests):
    try:
        img = Image.open(TICKET_TEMPLATE).convert("RGB").copy()
    except Exception:
        img = Image.new("RGB", (730, 350), color=(30, 30, 30))

    W, H = img.size

    # Чёрный прямоугольник занимает левые ~46% ширины и примерно с 30% по 90% высоты
    rect_x1 = int(W * 0.02)
    rect_y1 = int(H * 0.30)
    rect_y2 = int(H * 0.90)
    rect_h = rect_y2 - rect_y1

    draw = ImageDraw.Draw(img)

    font_paths = [
        "C:/Windows/Fonts/arial.ttf",
        "C:/Windows/Fonts/Arial.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "arial.ttf",
    ]

    def load_font(size):
        for path in font_paths:
            try:
                return ImageFont.truetype(path, size)
            except Exception:
                continue
        return ImageFont.load_default()

    # Чуть свободнее по вертикали и с меньшим кеглем для длинных строк
    font_big = load_font(max(10, int(H * 0.070)))
    font_med = load_font(max(9, int(H * 0.060)))
    font_small = load_font(max(8, int(H * 0.048)))

    x = rect_x1 + int(W * 0.02)
    # 4 строки с равными промежутками внутри тёмного блока
    step = max(1, rect_h // 5)
    y0 = rect_y1 + int(step * 0.55)

    draw.text((x, y0), name, font=font_big, fill="white")
    draw.text((x, y0 + step), f"{date_str}  {time_str}", font=font_med, fill="white")
    draw.text((x, y0 + step * 2), location, font=font_small, fill="white")
    draw.text((x, y0 + step * 3), guests_word(guests), font=font_med, fill="white")

    buf = BytesIO()
    img.save(buf, format="JPEG", quality=90)
    buf.seek(0)
    return buf
