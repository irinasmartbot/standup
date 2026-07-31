"""Списки подтверждённых броней «Проверка» для менеджера: ?start=new_stata."""

from __future__ import annotations

import re
from html import escape

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder

from bot.config import STATS_MANAGER_IDS, TEST_ADMIN_IDS
from bot.db.crud import get_manager_stata_bookings_for_date, get_manager_stata_dates

router = Router()

DATE_RE = re.compile(r"^\d{2}\.\d{2}\.\d{4}$")
TG_TEXT_LIMIT = 3500
NAME_COL_MIN = 12
NAME_COL_MAX = 22

CHOOSE_DATE_TEXT = (
    "Выбери дату, на которую нужно получить список гостей "
    "с билетом на проверку материала.\n\n"
    "Если нужной даты нет — напиши в формате DD.MM.YYYY"
)
EMPTY_TEXT = "Нет подтверждённых броней на указанную дату"
ACCESS_DENIED = "Нет доступа к этой команде."


class ManagerStata(StatesGroup):
    choosing_date = State()


def can_use_manager_stata(user_id: int | None) -> bool:
    if not user_id:
        return False
    allowed = STATS_MANAGER_IDS or TEST_ADMIN_IDS
    if not allowed:
        return True
    return int(user_id) in allowed


def _back_keyboard():
    kb = InlineKeyboardBuilder()
    kb.button(text="⬅️ Назад к датам", callback_data="nst:back")
    kb.adjust(1)
    return kb.as_markup()


def _dates_keyboard(dates: list[str]):
    kb = InlineKeyboardBuilder()
    for day in dates[:16]:
        kb.button(text=day, callback_data=f"nst:d:{day}")
    kb.adjust(2)
    return kb.as_markup()


def _phone_digits(phone: str) -> str:
    return re.sub(r"\D+", "", phone or "")


def _tel_href(digits: str) -> str | None:
    if not digits:
        return None
    d = digits
    if len(d) == 11 and d.startswith("8"):
        d = "7" + d[1:]
    if not d.startswith("+"):
        d = "+" + d
    return f"tel:{d}"


def _phone_html(phone: str) -> str:
    digits = _phone_digits(phone)
    if not digits:
        return "—"
    href = _tel_href(digits)
    label = escape(digits)
    if not href:
        return label
    return f'<a href="{escape(href, quote=True)}">{label}</a>'


def _name_width(rows: list[dict]) -> int:
    lengths = [len((r.get("name") or "").strip() or "—") for r in rows]
    if not lengths:
        return NAME_COL_MIN
    return max(NAME_COL_MIN, min(NAME_COL_MAX, max(lengths)))


def _guest_line(row: dict, name_w: int) -> str:
    name = (row.get("name") or "").strip() or "—"
    if len(name) > name_w:
        name = name[: name_w - 1] + "…"
    pad = " " * max(0, name_w - len(name))
    guests = int(row.get("guests") or 0)
    return f"<code>{escape(name)}{pad}</code> {_phone_html(row.get('phone') or '')}  <code>{guests}</code>"


def _show_header(row: dict) -> str:
    time = (row.get("event_time") or "").strip()
    location = (row.get("location") or "").strip()
    address = (row.get("address") or "").strip()
    if address and location and address.lower().startswith(location.lower()):
        venue = address
    elif location and address:
        venue = f"{location}, {address}"
    else:
        venue = location or address
    if time and venue:
        return f"<b>{escape(time)} - {escape(venue)}</b>"
    if time:
        return f"<b>{escape(time)}</b>"
    return f"<b>{escape(venue or 'Шоу')}</b>"


def build_stata_report(rows: list[dict]) -> str:
    if not rows:
        return EMPTY_TEXT

    groups: list[tuple[dict, list[dict]]] = []
    current_key = None
    current_header_row = None
    current_rows: list[dict] = []

    for row in rows:
        key = (row.get("event_id"), row.get("event_time"), row.get("location"), row.get("address"))
        if key != current_key:
            if current_header_row is not None:
                groups.append((current_header_row, current_rows))
            current_key = key
            current_header_row = row
            current_rows = [row]
        else:
            current_rows.append(row)
    if current_header_row is not None:
        groups.append((current_header_row, current_rows))

    blocks: list[str] = []
    day_guests = 0
    for header_row, group_rows in groups:
        name_w = _name_width(group_rows)
        guests_sum = sum(int(r.get("guests") or 0) for r in group_rows)
        day_guests += guests_sum
        lines = [_show_header(header_row), ""]
        lines.extend(_guest_line(r, name_w) for r in group_rows)
        lines.append(f"<b>Итого:</b> {guests_sum}")
        blocks.append("\n".join(lines))

    if len(blocks) > 1:
        blocks.append(f"<b>Всего за день:</b> {day_guests}")
    return "\n\n".join(blocks)


def _split_text(text: str, limit: int = TG_TEXT_LIMIT) -> list[str]:
    if len(text) <= limit:
        return [text]
    chunks: list[str] = []
    rest = text
    while rest:
        if len(rest) <= limit:
            chunks.append(rest)
            break
        cut = rest.rfind("\n", 0, limit)
        if cut < limit // 2:
            cut = limit
        chunks.append(rest[:cut].rstrip())
        rest = rest[cut:].lstrip()
    return chunks


async def _answer_html(message: Message, text: str, reply_markup=None):
    await message.answer(
        text,
        reply_markup=reply_markup,
        parse_mode="HTML",
        disable_web_page_preview=True,
    )


async def _edit_html(message: Message, text: str, reply_markup=None):
    await message.edit_text(
        text,
        reply_markup=reply_markup,
        parse_mode="HTML",
        disable_web_page_preview=True,
    )


async def send_manager_stata_start(message: Message, state: FSMContext):
    if not can_use_manager_stata(message.from_user.id if message.from_user else None):
        await message.answer(ACCESS_DENIED)
        return
    await state.set_state(ManagerStata.choosing_date)
    dates = get_manager_stata_dates()
    if not dates:
        await message.answer(
            CHOOSE_DATE_TEXT + "\n\n(В афише пока нет ближайших дат — напиши дату вручную.)",
        )
        return
    await message.answer(CHOOSE_DATE_TEXT, reply_markup=_dates_keyboard(dates))


async def _show_dates(target: Message, state: FSMContext, *, edit: bool = False):
    await state.set_state(ManagerStata.choosing_date)
    dates = get_manager_stata_dates()
    markup = _dates_keyboard(dates) if dates else None
    text = CHOOSE_DATE_TEXT
    if not dates:
        text += "\n\n(В афише пока нет ближайших дат — напиши дату вручную.)"
    if edit:
        try:
            await target.edit_text(text, reply_markup=markup)
            return
        except Exception:
            pass
    await target.answer(text, reply_markup=markup)


async def _send_report(message: Message, state: FSMContext, event_date: str, *, edit: bool = False):
    await state.set_state(ManagerStata.choosing_date)
    rows = get_manager_stata_bookings_for_date(event_date)
    text = build_stata_report(rows)
    chunks = _split_text(text)
    markup = _back_keyboard()

    if edit and len(chunks) == 1:
        try:
            await _edit_html(message, chunks[0], reply_markup=markup)
            return
        except Exception as exc:
            if "message is not modified" in str(exc).lower():
                return

    if edit and len(chunks) > 1:
        try:
            await _edit_html(message, chunks[0])
        except Exception:
            await _answer_html(message, chunks[0])
        for idx, chunk in enumerate(chunks[1:], start=1):
            is_last = idx == len(chunks) - 1
            await _answer_html(message, chunk, reply_markup=markup if is_last else None)
        return

    for idx, chunk in enumerate(chunks):
        is_last = idx == len(chunks) - 1
        await _answer_html(message, chunk, reply_markup=markup if is_last else None)


@router.callback_query(F.data == "nst:back")
async def nst_back(call: CallbackQuery, state: FSMContext):
    if not can_use_manager_stata(call.from_user.id if call.from_user else None):
        await call.answer(ACCESS_DENIED, show_alert=True)
        return
    await _show_dates(call.message, state, edit=True)
    await call.answer()


@router.callback_query(F.data.startswith("nst:d:"))
async def nst_date(call: CallbackQuery, state: FSMContext):
    if not can_use_manager_stata(call.from_user.id if call.from_user else None):
        await call.answer(ACCESS_DENIED, show_alert=True)
        return
    event_date = call.data.split(":", 2)[2]
    if not DATE_RE.match(event_date or ""):
        await call.answer("Некорректная дата", show_alert=True)
        return
    await _send_report(call.message, state, event_date, edit=True)
    await call.answer()


@router.message(ManagerStata.choosing_date, F.chat.type == "private", F.text)
async def nst_typed_date(message: Message, state: FSMContext):
    if not can_use_manager_stata(message.from_user.id if message.from_user else None):
        await message.answer(ACCESS_DENIED)
        return
    raw = (message.text or "").strip()
    if not DATE_RE.match(raw):
        await message.answer("Нужна дата в формате DD.MM.YYYY, например 31.07.2026")
        return
    await _send_report(message, state, raw, edit=False)
