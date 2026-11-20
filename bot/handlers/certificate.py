"""Telegram: вход по ссылке сертификата, фото и бронь без билета."""

from __future__ import annotations

import logging

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.base import StorageKey
from aiogram.types import CallbackQuery, KeyboardButton, Message, ReplyKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder

from bot.config import MODERATION_CHAT_ID, bot
from bot.db.cert_store import get_draft
from bot.services.cert_codes import looks_like_certificate_start, normalize_certificate_code
from bot.services.cert_flow import (
    ask_dates,
    ask_events,
    ask_format,
    begin,
    begin_transfer,
    cancel_booking,
    confirm_booking,
    draft_for,
    reserve_event,
    review,
    save_name,
    save_phone,
    submit_number,
    submit_photo,
)
from bot.services.cert_texts import APPROVED_TEXT, PHOTO_ASK_TEMPLATE, REJECT_TEXT

logger = logging.getLogger(__name__)
router = Router()


class CertState(StatesGroup):
    number = State()
    photo = State()
    name = State()
    phone = State()
    pick = State()


_EXPECT_STATE = {
    "number": CertState.number,
    "photo": CertState.photo,
    "name": CertState.name,
    "phone": CertState.phone,
    "buttons": CertState.pick,
}


def _markup(step: dict):
    if step.get("expect") == "phone":
        return ReplyKeyboardMarkup(
            keyboard=[[KeyboardButton(text="Отправить телефон", request_contact=True)]],
            resize_keyboard=True,
            one_time_keyboard=True,
        )
    buttons = step.get("buttons") or []
    if not buttons:
        return None
    kb = InlineKeyboardBuilder()
    for label, data in buttons:
        kb.button(text=label, callback_data=data)
    kb.adjust(1)
    return kb.as_markup()


async def _show(message: Message, state: FSMContext, step: dict) -> None:
    expect = step.get("expect") or "none"
    if expect in _EXPECT_STATE:
        await state.set_state(_EXPECT_STATE[expect])
    else:
        await state.clear()
    await message.answer(step["text"], reply_markup=_markup(step))


async def begin_telegram(message: Message, state: FSMContext, raw: str) -> None:
    step = await begin(
        channel="telegram",
        raw=raw,
        telegram_id=message.from_user.id,
    )
    await _show(message, state, step)


@router.message(
    F.text.func(lambda t: (t or "").casefold().strip() in {"сертификат", "сертификаты"}),
    F.chat.type == "private",
)
async def manual_certificate_start(message: Message, state: FSMContext):
    await begin_telegram(message, state, "msscert")


def _is_moderation(chat_id: int) -> bool:
    return bool(MODERATION_CHAT_ID) and str(chat_id) == str(MODERATION_CHAT_ID)


async def _set_guest_state(storage, user_id: int, new_state) -> None:
    key = StorageKey(bot_id=bot.id, chat_id=int(user_id), user_id=int(user_id))
    await storage.set_state(key, new_state)


@router.message(CertState.number, F.text, F.chat.type == "private")
async def on_number(message: Message, state: FSMContext):
    session = get_draft(telegram_id=message.from_user.id)
    if not session:
        await state.clear()
        return
    step = await submit_number(session, message.text or "")
    await _show(message, state, step)


@router.message(CertState.photo, F.photo, F.chat.type == "private")
async def on_photo(message: Message, state: FSMContext):
    session = get_draft(telegram_id=message.from_user.id)
    if not session:
        await state.clear()
        return
    photo = message.photo[-1]
    user = message.from_user
    step = await submit_photo(
        session,
        photo=photo.file_id,
        photo_ref=f"tg_file:{photo.file_id}",
        username=user.username or "",
        full_name=user.full_name or "",
    )
    await _show(message, state, step)


@router.message(CertState.photo, F.text, F.chat.type == "private")
async def on_photo_text(message: Message, state: FSMContext):
    session = get_draft(telegram_id=message.from_user.id)
    if not session:
        await state.clear()
        return
    if normalize_certificate_code(message.text or ""):
        step = await submit_number(session, message.text or "")
    else:
        code = session.get("code") or ""
        step = {
            "text": PHOTO_ASK_TEMPLATE.format(code=code) if code else "Пришли фото сертификата.",
            "expect": "photo",
            "buttons": [],
        }
    await _show(message, state, step)


@router.message(CertState.name, F.text, F.chat.type == "private")
async def on_name(message: Message, state: FSMContext):
    session = get_draft(telegram_id=message.from_user.id)
    if not session:
        await state.clear()
        return
    step = await save_name(session, message.text or "")
    await _show(message, state, step)


@router.message(CertState.phone, F.text | F.contact, F.chat.type == "private")
async def on_phone(message: Message, state: FSMContext):
    session = get_draft(telegram_id=message.from_user.id)
    if not session:
        await state.clear()
        return
    raw = message.contact.phone_number if message.contact else (message.text or "")
    step = await save_phone(session, raw)
    await _show(message, state, step)


async def _draft_or_hint(call: CallbackQuery) -> dict | None:
    session = draft_for(telegram_id=call.from_user.id)
    if session:
        return session
    await call.answer("Начни сертификат по ссылке со страницы ещё раз", show_alert=True)
    return None


@router.callback_query(F.data.startswith("cert_fmt:"))
async def on_format(call: CallbackQuery, state: FSMContext):
    fmt = call.data.split(":", 1)[1]
    session = await _draft_or_hint(call)
    if not session:
        return
    if fmt == "back":
        step = await ask_format()
    else:
        step = await ask_dates(session, fmt)
    await _show(call.message, state, step)
    await call.answer()


@router.callback_query(F.data.startswith("cert_day:"))
async def on_day(call: CallbackQuery, state: FSMContext):
    _, fmt, day = call.data.split(":", 2)
    session = await _draft_or_hint(call)
    if not session:
        return
    step = await ask_events(session, fmt, day)
    await _show(call.message, state, step)
    await call.answer()


@router.callback_query(F.data.startswith("cert_ev:"))
async def on_event(call: CallbackQuery, state: FSMContext):
    session = await _draft_or_hint(call)
    if not session:
        return
    event_id = int(call.data.split(":", 1)[1])
    step = await reserve_event(session, event_id)
    await _show(call.message, state, step)
    await call.answer()


@router.callback_query(F.data.startswith("cert_cf:"))
async def on_confirm(call: CallbackQuery, state: FSMContext):
    booking_id = int(call.data.split(":", 1)[1])
    step = await confirm_booking(booking_id, telegram_id=call.from_user.id)
    await _show(call.message, state, step)
    await call.answer()


@router.callback_query(F.data.startswith("cert_cx:"))
async def on_cancel(call: CallbackQuery, state: FSMContext):
    booking_id = int(call.data.split(":", 1)[1])
    await cancel_for_telegram(call, booking_id, state)


async def cancel_for_telegram(call: CallbackQuery, booking_id: int, state: FSMContext | None = None) -> None:
    step = await cancel_booking(booking_id, telegram_id=call.from_user.id)
    if state is not None:
        await _show(call.message, state, step)
    else:
        await call.message.answer(step["text"], reply_markup=_markup(step))
    await call.answer()


@router.callback_query(F.data.startswith("cert_mv:"))
async def on_move(call: CallbackQuery, state: FSMContext):
    booking_id = int(call.data.split(":", 1)[1])
    step = await begin_transfer(booking_id, telegram_id=call.from_user.id)
    await _show(call.message, state, step)
    await call.answer()


@router.callback_query(F.data.startswith("cert_mod_ok_") | F.data.startswith("cert_mod_no_"))
async def on_review(call: CallbackQuery, state: FSMContext):
    if not call.message or not _is_moderation(call.message.chat.id):
        await call.answer("Недоступно", show_alert=True)
        return
    approve = call.data.startswith("cert_mod_ok_")
    session_id = int(call.data.rsplit("_", 1)[1])
    session, text = await review(
        session_id,
        approve=approve,
        reviewer_id=call.from_user.id,
    )
    if text not in {APPROVED_TEXT, REJECT_TEXT}:
        await call.answer(text[:180], show_alert=True)
        return
    try:
        await call.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass
    mark = "принято" if approve else "отклонено"
    try:
        await call.message.edit_caption(caption=(call.message.caption or "") + f"\n\n{mark}")
    except Exception:
        pass
    await call.answer("Готово")
    if not session:
        return
    if session.get("channel") == "vk" and session.get("vk_id"):
        from bot.vk.raffle import send_vk_text

        await send_vk_text(int(session["vk_id"]), text)
        return
    guest_id = session.get("telegram_id")
    if not guest_id:
        return
    guest_state = CertState.name if approve else CertState.photo
    try:
        await _set_guest_state(state.storage, int(guest_id), guest_state)
    except Exception:
        logger.exception("certificate guest fsm failed telegram_id=%s", guest_id)
    await bot.send_message(int(guest_id), text)


def is_certificate_start(raw: str) -> bool:
    return looks_like_certificate_start(raw)
