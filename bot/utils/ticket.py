from datetime import datetime, timedelta, timezone
from io import BytesIO
import glob
import json
import os
import time

from PIL import Image, ImageDraw, ImageFont

from bot.config import TICKET_TEMPLATE, TICKET_TEMPLATES_DIR

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


def event_already_passed(date_str, time_str) -> bool:
    """True if event date+time is strictly before now (MSK, naive)."""
    event_dt = parse_event_datetime(date_str, time_str)
    if not event_dt:
        return False
    return event_dt < now_msk().replace(tzinfo=None)


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


# Эталон: макет «Лилия Васильевна». «БИЛЕТ» уже на PNG, бот слово не пишет.
_GOLD = (248, 200, 90)
_WHITE = (255, 255, 255)
_ADDR = (220, 217, 231)
_LAYOUT_W = 730
_LAYOUT_H = 380
_CX = 190
_NAME_CY = 131
_DATE_CY = 175
_ADDR_CY = (222, 246, 268)
_GUESTS_CY = 310
_MAX_NAME_W = 300
_MAX_W = 318
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_FONT_DIR = os.path.join(_PROJECT_ROOT, "fonts")
_BOLD_FONTS = (
    os.path.join(_FONT_DIR, "segoeprb.ttf"),
    "C:/Windows/Fonts/segoeprb.ttf",
    "C:/Windows/Fonts/arialbd.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
)
_REGULAR_FONTS = (
    os.path.join(_FONT_DIR, "segoepr.ttf"),
    "C:/Windows/Fonts/segoepr.ttf",
    "C:/Windows/Fonts/arial.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
)
_FEMALE_MARKERS = ("Lilac_Pearls", "Lavender_Velvet", "Turquoise_Diva")
# Имена на «а»/«я», которые мужские.
_MALE_A_NAMES = {
    "никита", "илья", "кузьма", "лука", "фома", "данила", "савва", "лева",
}


def _load_font(paths, size):
    for path in paths:
        try:
            return ImageFont.truetype(path, size)
        except Exception:
            continue
    return ImageFont.load_default()


def _line_metrics(draw, text, font):
    bbox = draw.textbbox((0, 0), text, font=font)
    return bbox[2] - bbox[0], bbox[3] - bbox[1], bbox


def _wrap_address(draw, text, font, max_w, max_lines=3):
    text = (text or "").strip()
    if not text:
        return []
    parts = [p.strip() for p in text.split(",") if p.strip()]
    pieces = [f"{p}," for p in parts[:-1]] + ([parts[-1]] if parts else [])
    if not pieces:
        return [line for line in _wrap_text(draw, text, font, max_w, max_lines) if line]
    lines, current = [], ""
    i = 0
    while i < len(pieces):
        piece = pieces[i]
        candidate = f"{current} {piece}".strip() if current else piece
        if _line_metrics(draw, candidate, font)[0] <= max_w:
            current = candidate
            i += 1
            continue
        if current and len(lines) < max_lines - 1:
            lines.append(current)
            current = ""
            continue
        rest = " ".join(([current] if current else []) + pieces[i:])
        lines.append(rest.strip())
        return lines[:max_lines]
    if current:
        lines.append(current)
    return lines[:max_lines]


def _draw_centered(draw, text, font, fill, cx, cy, tracking=0):
    if tracking and text and len(text) > 1:
        widths = [_line_metrics(draw, ch, font)[0] for ch in text]
        total = sum(widths) + tracking * (len(text) - 1)
        height = _line_metrics(draw, text, font)[1]
        x = cx - total / 2
        y = cy - height / 2 - draw.textbbox((0, 0), text, font=font)[1]
        for ch, width in zip(text, widths):
            draw.text(
                (x - draw.textbbox((0, 0), ch, font=font)[0], y),
                ch,
                font=font,
                fill=fill,
            )
            x += width + tracking
        return
    width, height, bbox = _line_metrics(draw, text, font)
    draw.text((cx - width / 2 - bbox[0], cy - height / 2 - bbox[1]), text, font=font, fill=fill)


def _address_centers(count, sy):
    top, mid, bottom = (_ADDR_CY[0] * sy, _ADDR_CY[1] * sy, _ADDR_CY[2] * sy)
    if count <= 1:
        return [mid]
    if count == 2:
        return [top, bottom]
    if count == 3:
        return [top, mid, bottom]
    span = bottom - top
    return [top + span * i / (count - 1) for i in range(count)]


def list_ticket_templates(directory: str | None = None) -> list[str]:
    root = directory or TICKET_TEMPLATES_DIR
    if not root or not os.path.isdir(root):
        return []
    files = sorted(glob.glob(os.path.join(root, "*.png")))
    files += [path for path in sorted(glob.glob(os.path.join(root, "*.jpg"))) if path not in files]
    return files


def ticket_gender(name: str) -> str:
    """f — кошка, m — кот. Берём первое слово имени."""
    parts = (name or "").strip().split()
    token = parts[0].lower().replace("ё", "е") if parts else ""
    token = "".join(ch for ch in token if ch.isalpha())
    if token in _MALE_A_NAMES:
        return "m"
    if token.endswith(("а", "я")):
        return "f"
    return "m"


def _pool_for_gender(templates: list[str], gender: str) -> list[str]:
    females = [path for path in templates if any(marker in os.path.basename(path) for marker in _FEMALE_MARKERS)]
    males = [path for path in templates if path not in females]
    pool = females if gender == "f" else males
    return pool or templates


def _next_template(gender: str, templates: list[str]) -> str:
    pool = _pool_for_gender(templates, gender)
    state_path = os.path.join(_PROJECT_ROOT, "data", "ticket_cat_index.json")
    os.makedirs(os.path.dirname(state_path), exist_ok=True)
    lock_path = state_path + ".lock"
    for _ in range(50):
        try:
            fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            break
        except FileExistsError:
            time.sleep(0.05)
    else:
        fd = None
    try:
        state = {"f": 0, "m": 0}
        if os.path.isfile(state_path):
            try:
                loaded = json.loads(open(state_path, encoding="utf-8").read())
                if isinstance(loaded, dict):
                    state.update({k: int(loaded.get(k, 0)) for k in ("f", "m")})
            except (OSError, ValueError, TypeError):
                pass
        index = state.get(gender, 0) % len(pool)
        chosen = pool[index]
        state[gender] = index + 1
        with open(state_path, "w", encoding="utf-8") as fh:
            json.dump(state, fh)
        return chosen
    finally:
        if fd is not None:
            os.close(fd)
            try:
                os.remove(lock_path)
            except OSError:
                pass


def _pick_ticket_template(name: str, template_path: str | None = None) -> str:
    if template_path and os.path.isfile(template_path):
        return template_path
    templates = list_ticket_templates()
    if not templates:
        return TICKET_TEMPLATE
    return _next_template(ticket_gender(name), templates)


def generate_ticket(name, date_str, time_str, location, guests, template_path=None):
    chosen = _pick_ticket_template(name or "", template_path)
    try:
        img = Image.open(chosen).convert("RGB").copy()
    except Exception:
        img = Image.new("RGB", (_LAYOUT_W, _LAYOUT_H), color=(18, 8, 28))

    W, H = img.size
    sx = W / _LAYOUT_W
    sy = H / _LAYOUT_H
    cx = _CX * sx
    max_name_w = int(_MAX_NAME_W * sx)
    max_w = int(_MAX_W * sx)
    draw = ImageDraw.Draw(img)

    def load_bold(size):
        return _load_font(_BOLD_FONTS, size)

    def load_regular(size):
        return _load_font(_REGULAR_FONTS, size)

    name_text = (name or "").strip()
    date_line = f"{date_str}  {time_str}".strip()
    guests_line = guests_word(guests)
    loc = (location or "").strip()

    font_name = _fit_font(draw, name_text or "Имя", load_bold, max_name_w, max(16, int(32 * sy)), 14)
    font_date = _fit_font(draw, date_line, load_bold, max_w, max(14, int(26 * sy)), 12)
    font_guests = _fit_font(draw, guests_line, load_bold, max_w, max(16, int(30 * sy)), 14)

    loc_max_lines = 3
    loc_size = max(13, int(20 * sy))
    min_loc = max(11, int(13 * sy))
    font_addr = load_regular(loc_size)
    loc_lines = _wrap_address(draw, loc, font_addr, max_w, loc_max_lines)
    while loc_size > min_loc and any(
        _line_metrics(draw, line, font_addr)[0] > max_w for line in loc_lines
    ):
        loc_size -= 1
        font_addr = load_regular(loc_size)
        loc_lines = _wrap_address(draw, loc, font_addr, max_w, loc_max_lines)

    if name_text:
        _draw_centered(draw, name_text, font_name, _WHITE, cx, _NAME_CY * sy)
    else:
        _, height, _bbox = _line_metrics(draw, "А", font_name)
        y = _NAME_CY * sy + height / 2 - 2
        x1 = cx - max_name_w / 2
        draw.line((x1, y, x1 + max_name_w * 0.88, y), fill=(230, 230, 230), width=2)

    _draw_centered(draw, date_line, font_date, _WHITE, cx, _DATE_CY * sy, tracking=0.6)
    for line, line_cy in zip(loc_lines, _address_centers(len(loc_lines), sy)):
        _draw_centered(draw, line, font_addr, _ADDR, cx, line_cy)
    _draw_centered(draw, guests_line, font_guests, _GOLD, cx, _GUESTS_CY * sy)

    buf = BytesIO()
    img.save(buf, format="JPEG", quality=92)
    buf.seek(0)
    return buf
