"""Списки подтверждённых броней для менеджера.

- ?start=new_stata — Проверка материала
- ?start=new_stata_rozygr — розыгрыш (бронь на BEST)
"""

from __future__ import annotations

import re
from html import escape

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder

from bot.db.crud import get_manager_stata_bookings_for_date, get_manager_stata_dates

router = Router()

DATE_RE = re.compile(r"^\d{2}\.\d{2}\.\d{4}$")
TG_TEXT_LIMIT = 3500

CHOOSE_DATE_TEXT_PROVERKA = (
    "Выбери дату, на которую нужно получить список гостей "
    "с билетом на проверку материала.\n\n"
    "Если нужной даты нет — напиши в формате DD.MM.YYYY"
)
CHOOSE_DATE_TEXT_ROZYGR = (
    "Выбери дату, на которую нужно получить список гостей "
    "с билетом по розыгрышу.\n\n"
    "Если нужной даты нет — напиши в формате DD.MM.YYYY"
)
EMPTY_TEXT_PROVERKA = "Нет подтверждённых броней на указанную дату"
EMPTY_TEXT_ROZYGR = "Нет подтверждённых броней розыгрыша на указанную дату"

# mode -> (callback prefix, event_format, booking_format, choose text, empty text)
_MODE = {
    "proverka": ("nst", "proverka", "proverka", CHOOSE_DATE_TEXT_PROVERKA, EMPTY_TEXT_PROVERKA),
    "rozygr": ("nstr", "best", "rozygrysh", CHOOSE_DATE_TEXT_ROZYGR, EMPTY_TEXT_ROZYGR),
}


class ManagerStata(StatesGroup):
    choosing_date = State()


class ManagerStataRozygr(StatesGroup):
    choosing_date = State()


def _mode_cfg(mode: str):
    return _MODE.get(mode) or _MODE["proverka"]


def _state_for_mode(mode: str):
    return ManagerStataRozygr.choosing_date if mode == "rozygr" else ManagerStata.choosing_date


def _back_keyboard(mode: str):
    prefix, *_ = _mode_cfg(mode)
    kb = InlineKeyboardBuilder()
    kb.button(text="⬅️ Назад к датам", callback_data=f"{prefix}:back")
    kb.adjust(1)
    return kb.as_markup()


def _dates_keyboard(dates: list[str], mode: str):
    prefix, *_ = _mode_cfg(mode)
    kb = InlineKeyboardBuilder()
    for day in dates[:16]:
        kb.button(text=day, callback_data=f"{prefix}:d:{day}")
    kb.adjust(2)
    return kb.as_markup()


def _phone_digits(phone: str) -> str:
    return re.sub(r"\D+", "", phone or "")


def _phone_plus(phone: str) -> str:
    """Формат +7926… — Telegram сам делает номер кликабельным."""
    digits = _phone_digits(phone)
    if not digits:
        return "—"
    if len(digits) == 11 and digits.startswith("8"):
        digits = "7" + digits[1:]
    if digits.startswith("7") and not digits.startswith("+"):
        return f"+{digits}"
    if digits.startswith("+"):
        return digits
    return f"+{digits}"


def _guest_line(row: dict) -> str:
    name = (row.get("name") or "").strip() or "—"
    guests = int(row.get("guests") or 0)
    phone = _phone_plus(row.get("phone") or "")
    return f"{escape(name)} · {escape(phone)} · {guests}"


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
        return f"{escape(time)} - {escape(venue)}"
    if time:
        return escape(time)
    return escape(venue or "Шоу")


def build_stata_report(rows: list[dict], *, empty_text: str = EMPTY_TEXT_PROVERKA) -> str:
    if not rows:
        return empty_text

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
        guests_sum = sum(int(r.get("guests") or 0) for r in group_rows)
        day_guests += guests_sum
        lines = [_show_header(header_row), ""]
        lines.extend(_guest_line(r) for r in group_rows)
        lines.append(f"Итого: {guests_sum}")
        blocks.append("\n".join(lines))

    if len(blocks) > 1:
        blocks.append(f"Всего за день: {day_guests}")
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


async def _start_for_mode(message: Message, state: FSMContext, mode: str):
    await state.set_state(_state_for_mode(mode))
    await state.update_data(stata_mode=mode)
    _, event_format, _, choose_text, _ = _mode_cfg(mode)
    dates = get_manager_stata_dates(event_format=event_format)
    if not dates:
        await message.answer(
            choose_text + "\n\n(В афише пока нет ближайших дат — напиши дату вручную.)",
        )
        return
    await message.answer(choose_text, reply_markup=_dates_keyboard(dates, mode))


async def send_manager_stata_start(message: Message, state: FSMContext):
    await _start_for_mode(message, state, "proverka")


async def send_manager_stata_rozygr_start(message: Message, state: FSMContext):
    await _start_for_mode(message, state, "rozygr")


async def _show_dates(target: Message, state: FSMContext, mode: str, *, edit: bool = False):
    await state.set_state(_state_for_mode(mode))
    await state.update_data(stata_mode=mode)
    _, event_format, _, choose_text, _ = _mode_cfg(mode)
    dates = get_manager_stata_dates(event_format=event_format)
    markup = _dates_keyboard(dates, mode) if dates else None
    text = choose_text
    if not dates:
        text += "\n\n(В афише пока нет ближайших дат — напиши дату вручную.)"
    if edit:
        try:
            await target.edit_text(text, reply_markup=markup)
            return
        except Exception:
            pass
    await target.answer(text, reply_markup=markup)


async def _send_report(message: Message, state: FSMContext, event_date: str, mode: str, *, edit: bool = False):
    await state.set_state(_state_for_mode(mode))
    await state.update_data(stata_mode=mode)
    _, event_format, booking_format, _, empty_text = _mode_cfg(mode)
    rows = get_manager_stata_bookings_for_date(
        event_date,
        booking_format=booking_format,
        event_format=event_format,
    )
    text = build_stata_report(rows, empty_text=empty_text)
    chunks = _split_text(text)
    markup = _back_keyboard(mode)

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
    await _show_dates(call.message, state, "proverka", edit=True)
    await call.answer()


@router.callback_query(F.data.startswith("nst:d:"))
async def nst_date(call: CallbackQuery, state: FSMContext):
    event_date = call.data.split(":", 2)[2]
    if not DATE_RE.match(event_date or ""):
        await call.answer("Некорректная дата", show_alert=True)
        return
    await _send_report(call.message, state, event_date, "proverka", edit=True)
    await call.answer()


@router.callback_query(F.data == "nstr:back")
async def nstr_back(call: CallbackQuery, state: FSMContext):
    await _show_dates(call.message, state, "rozygr", edit=True)
    await call.answer()


@router.callback_query(F.data.startswith("nstr:d:"))
async def nstr_date(call: CallbackQuery, state: FSMContext):
    event_date = call.data.split(":", 2)[2]
    if not DATE_RE.match(event_date or ""):
        await call.answer("Некорректная дата", show_alert=True)
        return
    await _send_report(call.message, state, event_date, "rozygr", edit=True)
    await call.answer()


@router.message(ManagerStata.choosing_date, F.chat.type == "private", F.text)
async def nst_typed_date(message: Message, state: FSMContext):
    raw = (message.text or "").strip()
    if not DATE_RE.match(raw):
        await message.answer("Нужна дата в формате DD.MM.YYYY, например 31.07.2026")
        return
    await _send_report(message, state, raw, "proverka", edit=False)


@router.message(ManagerStataRozygr.choosing_date, F.chat.type == "private", F.text)
async def nstr_typed_date(message: Message, state: FSMContext):
    raw = (message.text or "").strip()
    if not DATE_RE.match(raw):
        await message.answer("Нужна дата в формате DD.MM.YYYY, например 31.07.2026")
        return
    await _send_report(message, state, raw, "rozygr", edit=False)
