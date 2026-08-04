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


def format_ticket_place(location: str = "", address: str = "", *, full: bool = True) -> str:
    """Полный адрес на картинке билета (как в подписи). full оставлен для совместимости."""
    location = (location or "").strip()
    address = (address or "").strip()
    if address and location and not address.lower().startswith(location.lower()):
        return f"{location}, {address}"
    return address or location


def _text_width(draw: ImageDraw.ImageDraw, text: str, font: ImageFont.ImageFont) -> int:
    bbox = draw.textbbox((0, 0), text, font=font)
    return max(0, bbox[2] - bbox[0])


def _fit_font(draw, text: str, load_font, max_width: int, max_size: int, min_size: int = 8):
    """Подбирает кегль, чтобы строка влезла в max_width."""
    size = max_size
    while size >= min_size:
        font = load_font(size)
        if _text_width(draw, text, font) <= max_width:
            return font
        size -= 1
    return load_font(min_size)


def _wrap_text(draw, text: str, font, max_width: int, max_lines: int = 2) -> list[str]:
    """Перенос по словам; если не влезает — режет с многоточием на последней строке."""
    text = (text or "").strip()
    if not text:
        return [""]
    if _text_width(draw, text, font) <= max_width:
        return [text]

    words = text.replace(",", ", ").split()
    lines: list[str] = []
    current = ""
    for word in words:
        candidate = f"{current} {word}".strip() if current else word
        if _text_width(draw, candidate, font) <= max_width:
            current = candidate
            continue
        if current:
            lines.append(current)
            current = word
        else:
            # одно очень длинное «слово»
            lines.append(word)
            current = ""
        if len(lines) >= max_lines:
            break
    if current and len(lines) < max_lines:
        lines.append(current)
    if not lines:
        lines = [text]

    # Если слов осталось больше, чем строк — ужимаем последнюю с …
    joined_words = " ".join(words)
    used = " ".join(lines)
    if used != joined_words and lines:
        last = lines[-1].rstrip(" .,")
        while last and _text_width(draw, last + "…", font) > max_width:
            last = last[:-1].rstrip(" .,")
        lines[-1] = (last + "…") if last else "…"
    return lines[:max_lines]


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
    # Правый край текстового поля (чуть внутри тёмного блока шаблона).
    max_text_w = int(W * 0.42)

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

    font_big = _fit_font(draw, name or "", load_font, max_text_w, max(10, int(H * 0.070)))
    date_line = f"{date_str}  {time_str}"
    font_med = _fit_font(draw, date_line, load_font, max_text_w, max(9, int(H * 0.060)))
    guests_line = guests_word(guests)
    font_guests = _fit_font(draw, guests_line, load_font, max_text_w, max(9, int(H * 0.060)))

    # Адрес: для полного (розыгрыш) до 4 строк, кегль чуть ближе к дате.
    loc = (location or "").strip()
    loc_max_lines = 4 if len(loc) > 40 else 3
    loc_size = max(11, int(H * 0.055))
    min_loc = max(9, int(H * 0.038))
    font_small = load_font(loc_size)
    loc_lines = _wrap_text(draw, loc, font_small, max_text_w, max_lines=loc_max_lines)
    while loc_size > min_loc and any(
        _text_width(draw, line, font_small) > max_text_w for line in loc_lines
    ):
        loc_size -= 1
        font_small = load_font(loc_size)
        loc_lines = _wrap_text(draw, loc, font_small, max_text_w, max_lines=loc_max_lines)

    x = rect_x1 + int(W * 0.02)
    # 4–6 строк с равными промежутками внутри тёмного блока
    line_count = 3 + len(loc_lines)  # name, date, address lines, guests
    step = max(1, rect_h // (line_count + 1))
    y = rect_y1 + int(step * 0.45)

    draw.text((x, y), name or "", font=font_big, fill="white")
    y += step
    draw.text((x, y), date_line, font=font_med, fill="white")
    y += step
    for line in loc_lines:
        draw.text((x, y), line, font=font_small, fill="white")
        y += step
    draw.text((x, y), guests_line, font=font_guests, fill="white")

    buf = BytesIO()
    img.save(buf, format="JPEG", quality=90)
    buf.seek(0)
    return buf
