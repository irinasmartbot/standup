"""Списки активных броней «Проверка» для менеджера: ?start=new_stata."""

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
STATUS_LABEL = {
    "booked": "бронь",
    "confirmed": "билет",
}
TG_TEXT_LIMIT = 3500

CHOOSE_DATE_TEXT = (
    "Выбери дату, на которую нужно получить список активных броней "
    "на проверку материала.\n\n"
    "Если нужной даты нет — напиши в формате DD.MM.YYYY"
)
EMPTY_TEXT = "Нет активных броней на указанную дату"
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


def _guest_line(row: dict) -> str:
    name = (row.get("name") or "").strip() or "—"
    phone = _phone_digits(row.get("phone") or "") or "—"
    guests = int(row.get("guests") or 0)
    status = STATUS_LABEL.get(row.get("status") or "", row.get("status") or "бронь")
    return f"{escape(name)} {escape(phone)} {guests} / {escape(status)}"


def _show_header(row: dict) -> str:
    time = (row.get("event_time") or "").strip()
    location = (row.get("location") or "").strip()
    address = (row.get("address") or "").strip()
    venue = ", ".join(part for part in (location, address) if part)
    if time and venue:
        return f"{escape(time)} - {escape(venue)}"
    if time:
        return escape(time)
    return escape(venue or "Шоу")


def build_stata_report(rows: list[dict]) -> str:
    if not rows:
        return EMPTY_TEXT

    blocks: list[str] = []
    current_key = None
    current_lines: list[str] = []
    current_guests = 0
    day_guests = 0

    def flush():
        nonlocal current_lines, current_guests
        if not current_lines:
            return
        current_lines.append(f"Итого: {current_guests}")
        blocks.append("\n".join(current_lines))
        current_lines = []
        current_guests = 0

    for row in rows:
        key = (row.get("event_id"), row.get("event_time"), row.get("location"), row.get("address"))
        if key != current_key:
            flush()
            current_key = key
            current_lines = [_show_header(row), ""]
        current_lines.append(_guest_line(row))
        guests = int(row.get("guests") or 0)
        current_guests += guests
        day_guests += guests

    flush()

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
            await message.edit_text(chunks[0], reply_markup=markup)
            return
        except Exception as exc:
            if "message is not modified" in str(exc).lower():
                return

    if edit and len(chunks) > 1:
        try:
            await message.edit_text(chunks[0])
        except Exception:
            await message.answer(chunks[0])
        for idx, chunk in enumerate(chunks[1:], start=1):
            is_last = idx == len(chunks) - 1
            await message.answer(chunk, reply_markup=markup if is_last else None)
        return

    for idx, chunk in enumerate(chunks):
        is_last = idx == len(chunks) - 1
        await message.answer(chunk, reply_markup=markup if is_last else None)


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
