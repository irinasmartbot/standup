"""Telegram: вход по ссылке сертификата, фото и бронь без билета."""

from __future__ import annotations

import logging

from aiogram import F, Router
from aiogram.dispatcher.event.bases import SkipHandler
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.base import StorageKey
from aiogram.types import CallbackQuery, FSInputFile, KeyboardButton, Message, ReplyKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder

from bot.config import MODERATION_CHAT_ID, bot
from bot.db.cert_store import get_draft
from bot.services.cert_codes import looks_like_certificate_start, normalize_certificate_code
from bot.services.cert_flow import (
    ask_dates,
    ask_events,
    ask_format,
    ask_phone_manual,
    begin,
    begin_transfer,
    cancel_booking,
    confirm_booking,
    draft_for,
    number_hint_photo_path,
    reserve_event,
    review,
    save_name,
    save_phone,
    show_event_card,
    submit_number,
    submit_photo,
    use_saved_phone,
)
from bot.services.cert_texts import APPROVED_TEXT, NUMBER_HINT_SCREEN, PHOTO_ASK_TEMPLATE, REJECT_TEXT
from bot.utils.reply_keyboard import clear_reply_keyboard, mark_reply_keyboard

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


def _parse_mode(step: dict) -> str | None:
    return "HTML" if step.get("html", True) else None


def _markup(step: dict, *, chat_id: int | None = None):
    if step.get("expect") == "phone":
        if chat_id:
            mark_reply_keyboard(chat_id)
        return ReplyKeyboardMarkup(
            keyboard=[[KeyboardButton(text="📱 Отправить телефон", request_contact=True)]],
            resize_keyboard=True,
            one_time_keyboard=True,
        )
    buttons = step.get("buttons") or []
    if not buttons:
        return None
    kb = InlineKeyboardBuilder()
    for label, data in buttons:
        kb.button(text=label, callback_data=data)
    layout = step.get("layout")
    if layout == "dates" and len(buttons) > 1:
        n = len(buttons) - 1
        widths = [2] * (n // 2)
        if n % 2:
            widths.append(1)
        widths.append(1)
        kb.adjust(*widths)
    else:
        kb.adjust(1)
    return kb.as_markup()


async def _purge_cert_ui(chat_id: int, state: FSMContext, extra=()) -> None:
    data = await state.get_data()
    ids = list(data.get("cert_ui") or [])
    for mid in extra:
        if mid:
            ids.append(int(mid))
    seen: set[int] = set()
    for mid in ids:
        if mid in seen:
            continue
        seen.add(mid)
        try:
            await bot.delete_message(chat_id, mid)
        except Exception:
            pass
    await state.update_data(cert_ui=[])


def _format_cover(event_format: str):
    from bot.handlers.formats import _elka_photo, _hitloto_photo, _random_format_photo

    if event_format == "hitloto":
        return _hitloto_photo()
    if event_format == "elka":
        return _elka_photo()
    return _random_format_photo()


async def _send_step(message: Message, step: dict):
    markup = _markup(step, chat_id=message.chat.id)
    parse_mode = _parse_mode(step)
    text = step.get("text") or ""
    screen = step.get("screen") or "text"
    if screen == NUMBER_HINT_SCREEN:
        path = number_hint_photo_path()
        if path:
            try:
                return await message.answer_photo(
                    photo=FSInputFile(path),
                    caption=text,
                    reply_markup=markup,
                    parse_mode=parse_mode,
                )
            except Exception:
                logger.exception("certificate number hint photo failed")
    if screen == "dates":
        photo = _format_cover(str(step.get("event_format") or ""))
        if photo:
            try:
                return await message.answer_photo(
                    photo=photo,
                    caption=text,
                    reply_markup=markup,
                    parse_mode=parse_mode,
                )
            except Exception:
                logger.exception("certificate dates cover failed")
    if screen == "event_card":
        event = step.get("event") or {}
        from bot.utils.event_poster import tg_send_event_card

        return await tg_send_event_card(
            message,
            event.get("image"),
            caption=text,
            reply_markup=markup,
            parse_mode=parse_mode,
        )
    return await message.answer(text, reply_markup=markup, parse_mode=parse_mode)


async def _show(
    message: Message,
    state: FSMContext,
    step: dict,
    *,
    extra_delete=(),
    drop_user: bool = False,
) -> None:
    expect = step.get("expect") or "none"
    if expect in _EXPECT_STATE:
        await state.set_state(_EXPECT_STATE[expect])
    elif expect == "none":
        await state.set_state(None)
    extra = list(extra_delete)
    if drop_user and message.message_id:
        extra.append(message.message_id)
    await _purge_cert_ui(message.chat.id, state, extra)
    if step.get("expect") != "phone":
        await clear_reply_keyboard(message)
    sent = await _send_step(message, step)
    if sent:
        await state.update_data(cert_ui=[sent.message_id])


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
    await begin_telegram(message, state, "certificate")


def _is_moderation(chat_id: int) -> bool:
    return bool(MODERATION_CHAT_ID) and str(chat_id) == str(MODERATION_CHAT_ID)


# card_message_id -> {session_id, prompt_message_id}
_PENDING_REJECT_BY_MSG: dict[int, dict] = {}


def is_pending_cert_reject_reply(reply_to_message_id: int) -> bool:
    if reply_to_message_id in _PENDING_REJECT_BY_MSG:
        return True
    return any(
        data.get("prompt_message_id") == reply_to_message_id
        for data in _PENDING_REJECT_BY_MSG.values()
    )


def _pending_reject_lookup(message_id: int):
    data = _PENDING_REJECT_BY_MSG.get(message_id)
    if data:
        return message_id, data
    for card_id, item in _PENDING_REJECT_BY_MSG.items():
        if item.get("prompt_message_id") == message_id:
            return card_id, item
    return None, None


async def _delete_mod_chat_messages(chat_id: int, *message_ids) -> None:
    for mid in message_ids:
        if not mid:
            continue
        try:
            await bot.delete_message(chat_id, mid)
        except Exception:
            pass


@router.message(CertState.number, F.text, F.chat.type == "private")
async def on_number(message: Message, state: FSMContext):
    session = get_draft(telegram_id=message.from_user.id)
    if not session:
        await state.clear()
        return
    step = await submit_number(session, message.text or "")
    await _show(message, state, step, drop_user=True)


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
    await _show(message, state, step, drop_user=True)


@router.message(CertState.phone, F.text | F.contact, F.chat.type == "private")
async def on_phone(message: Message, state: FSMContext):
    session = get_draft(telegram_id=message.from_user.id)
    if not session:
        await state.clear()
        return
    raw = message.contact.phone_number if message.contact else (message.text or "")
    step = await save_phone(session, raw)
    await _show(message, state, step, drop_user=True)


@router.message(CertState.pick, F.contact, F.chat.type == "private")
@router.message(CertState.pick, F.text, F.chat.type == "private")
async def on_pick_phone(message: Message, state: FSMContext):
    session = get_draft(telegram_id=message.from_user.id)
    if not session or session.get("state") != "need_phone":
        raise SkipHandler
    raw = message.contact.phone_number if message.contact else (message.text or "")
    step = await save_phone(session, raw)
    await _show(message, state, step, drop_user=True)


async def _draft_or_hint(call: CallbackQuery) -> dict | None:
    session = draft_for(telegram_id=call.from_user.id)
    if session:
        return session
    await call.answer("Начни сертификат по ссылке со страницы ещё раз", show_alert=True)
    return None


@router.callback_query(F.data.startswith("cert_ph:"))
async def on_phone_choice(call: CallbackQuery, state: FSMContext):
    session = await _draft_or_hint(call)
    if not session:
        return
    choice = call.data.split(":", 1)[1]
    if choice == "ok":
        step = await use_saved_phone(session)
    else:
        step = ask_phone_manual()
    extra = (call.message.message_id,) if call.message else ()
    await _show(call.message, state, step, extra_delete=extra)
    await call.answer()


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
    await _show(call.message, state, step, extra_delete=(call.message.message_id,))
    await call.answer()


@router.callback_query(F.data.startswith("cert_day:"))
async def on_day(call: CallbackQuery, state: FSMContext):
    _, fmt, day = call.data.split(":", 2)
    session = await _draft_or_hint(call)
    if not session:
        return
    step = await ask_events(session, fmt, day)
    await _show(call.message, state, step, extra_delete=(call.message.message_id,))
    await call.answer()


@router.callback_query(F.data.startswith("cert_card:"))
async def on_card(call: CallbackQuery, state: FSMContext):
    session = await _draft_or_hint(call)
    if not session:
        return
    step = await show_event_card(session, int(call.data.split(":", 1)[1]))
    await _show(call.message, state, step, extra_delete=(call.message.message_id,))
    await call.answer()


@router.callback_query(F.data.startswith("cert_ev:"))
async def on_event(call: CallbackQuery, state: FSMContext):
    session = await _draft_or_hint(call)
    if not session:
        return
    event_id = int(call.data.split(":", 1)[1])
    step = await reserve_event(session, event_id)
    await _show(call.message, state, step, extra_delete=(call.message.message_id,))
    await call.answer()


@router.callback_query(F.data.startswith("cert_cf:"))
async def on_confirm(call: CallbackQuery, state: FSMContext):
    booking_id = int(call.data.split(":", 1)[1])
    step = await confirm_booking(booking_id, telegram_id=call.from_user.id)
    await _show(call.message, state, step, extra_delete=(call.message.message_id,))
    await call.answer()


@router.callback_query(F.data.startswith("cert_cx:"))
async def on_cancel(call: CallbackQuery, state: FSMContext):
    booking_id = int(call.data.split(":", 1)[1])
    await cancel_for_telegram(call, booking_id, state)


async def cancel_for_telegram(call: CallbackQuery, booking_id: int, state: FSMContext | None = None) -> None:
    step = await cancel_booking(booking_id, telegram_id=call.from_user.id)
    extra = (call.message.message_id,) if call.message else ()
    if state is not None:
        await _show(call.message, state, step, extra_delete=extra)
    else:
        await call.message.answer(
            step["text"],
            reply_markup=_markup(step, chat_id=call.message.chat.id),
            parse_mode=_parse_mode(step),
        )
    await call.answer()


@router.callback_query(F.data.startswith("cert_mv:"))
async def on_move(call: CallbackQuery, state: FSMContext):
    booking_id = int(call.data.split(":", 1)[1])
    step = await begin_transfer(booking_id, telegram_id=call.from_user.id)
    await _show(call.message, state, step, extra_delete=(call.message.message_id,))
    await call.answer()


async def _notify_guest(session: dict, text: str, *, approve: bool, storage) -> None:
    if session.get("channel") == "vk" and session.get("vk_id"):
        from bot.vk.raffle import send_vk_text

        await send_vk_text(int(session["vk_id"]), text)
        return
    guest_id = session.get("telegram_id")
    if not guest_id:
        return
    guest_state = CertState.name if approve else CertState.photo
    key = StorageKey(bot_id=bot.id, chat_id=int(guest_id), user_id=int(guest_id))
    try:
        await storage.set_state(key, guest_state)
        data = await storage.get_data(key)
        for mid in data.get("cert_ui") or []:
            try:
                await bot.delete_message(int(guest_id), int(mid))
            except Exception:
                pass
    except Exception:
        logger.exception("certificate guest fsm failed telegram_id=%s", guest_id)
        data = {}
    sent = await bot.send_message(int(guest_id), text, parse_mode="HTML")
    try:
        await storage.set_data(key, {**data, "cert_ui": [sent.message_id]})
    except Exception:
        logger.exception("certificate guest ui save failed telegram_id=%s", guest_id)


async def _mark_card(
    chat_id: int,
    message_id: int,
    *,
    caption: str | None,
    approve: bool,
    reason: str = "",
) -> None:
    try:
        await bot.edit_message_reply_markup(
            chat_id=chat_id, message_id=message_id, reply_markup=None
        )
    except Exception:
        pass
    mark = "принято" if approve else "отклонено"
    if reason:
        mark += f"\nПричина: {reason}"
    try:
        await bot.edit_message_caption(
            chat_id=chat_id,
            message_id=message_id,
            caption=(caption or "") + f"\n\n{mark}",
        )
    except Exception:
        pass


async def _decide_review(
    *,
    session_id: int,
    approve: bool,
    reviewer_id: int,
    reason: str,
    chat_id: int,
    message_id: int,
    caption: str | None,
    storage,
) -> str:
    session, text = await review(
        session_id,
        approve=approve,
        reviewer_id=reviewer_id,
        reason=reason,
    )
    if text not in {APPROVED_TEXT, REJECT_TEXT}:
        return text
    guest_text = text
    if not approve and reason:
        guest_text = f"{REJECT_TEXT}\n\nКомментарий менеджера: {reason}"
    await _mark_card(
        chat_id,
        message_id,
        caption=caption,
        approve=approve,
        reason=reason,
    )
    if session:
        await _notify_guest(session, guest_text, approve=approve, storage=storage)
    return "ok"


@router.callback_query(F.data.startswith("cert_mod_ok_"))
async def on_mod_ok(call: CallbackQuery, state: FSMContext):
    if not call.message or not _is_moderation(call.message.chat.id):
        await call.answer("Недоступно", show_alert=True)
        return
    session_id = int(call.data.removeprefix("cert_mod_ok_"))
    pending = _PENDING_REJECT_BY_MSG.pop(call.message.message_id, None)
    if pending:
        await _delete_mod_chat_messages(call.message.chat.id, pending.get("prompt_message_id"))
    result = await _decide_review(
        session_id=session_id,
        approve=True,
        reviewer_id=call.from_user.id,
        reason="",
        chat_id=call.message.chat.id,
        message_id=call.message.message_id,
        caption=call.message.caption,
        storage=state.storage,
    )
    if result != "ok":
        await call.answer(result[:180], show_alert=True)
        return
    await call.answer("Готово")


@router.callback_query(F.data.startswith("cert_mod_no_reason_"))
async def on_mod_no_reason(call: CallbackQuery, state: FSMContext):
    if not call.message or not _is_moderation(call.message.chat.id):
        await call.answer("Недоступно", show_alert=True)
        return
    session_id = int(call.data.removeprefix("cert_mod_no_reason_"))
    card_msg_id = call.message.message_id
    old_pending = _PENDING_REJECT_BY_MSG.pop(card_msg_id, None)
    if old_pending:
        await _delete_mod_chat_messages(
            call.message.chat.id, old_pending.get("prompt_message_id")
        )
    prompt = await call.message.reply(
        "Напишите причину отказа, нажав «ответить» на сообщение с данным скрином\n\n"
        f"Заявка #{session_id}",
    )
    _PENDING_REJECT_BY_MSG[card_msg_id] = {
        "session_id": session_id,
        "prompt_message_id": prompt.message_id,
        "caption": call.message.caption or "",
    }
    await call.answer()


@router.callback_query(F.data.startswith("cert_mod_no_silent_"))
async def on_mod_no_silent(call: CallbackQuery, state: FSMContext):
    if not call.message or not _is_moderation(call.message.chat.id):
        await call.answer("Недоступно", show_alert=True)
        return
    session_id = int(call.data.removeprefix("cert_mod_no_silent_"))
    await _reject_silent(call, state, session_id)


@router.callback_query(F.data.startswith("cert_mod_no_"))
async def on_mod_no_legacy(call: CallbackQuery, state: FSMContext):
    if not call.message or not _is_moderation(call.message.chat.id):
        await call.answer("Недоступно", show_alert=True)
        return
    rest = call.data.removeprefix("cert_mod_no_")
    if rest.startswith("silent_") or rest.startswith("reason_"):
        return
    session_id = int(rest)
    await _reject_silent(call, state, session_id)


async def _reject_silent(call: CallbackQuery, state: FSMContext, session_id: int) -> None:
    pending = _PENDING_REJECT_BY_MSG.pop(call.message.message_id, None)
    if pending:
        await _delete_mod_chat_messages(call.message.chat.id, pending.get("prompt_message_id"))
    result = await _decide_review(
        session_id=session_id,
        approve=False,
        reviewer_id=call.from_user.id,
        reason="",
        chat_id=call.message.chat.id,
        message_id=call.message.message_id,
        caption=call.message.caption,
        storage=state.storage,
    )
    if result != "ok":
        await call.answer(result[:180], show_alert=True)
        return
    await call.answer("Отклонено")


@router.message(F.reply_to_message)
async def on_mod_reject_reason(message: Message, state: FSMContext):
    if not _is_moderation(message.chat.id):
        raise SkipHandler
    replied_id = message.reply_to_message.message_id
    card_msg_id, pending = _pending_reject_lookup(replied_id)
    if not pending and message.reply_to_message.reply_to_message:
        card_msg_id, pending = _pending_reject_lookup(
            message.reply_to_message.reply_to_message.message_id
        )
    if not pending:
        raise SkipHandler

    session_id = int(pending["session_id"])
    prompt_message_id = pending.get("prompt_message_id")
    reason = (message.text or "").strip()
    if not reason:
        await message.reply("Нужен текст причины. Ответьте реплаем на карточку с фото.")
        return

    _PENDING_REJECT_BY_MSG.pop(card_msg_id, None)
    result = await _decide_review(
        session_id=session_id,
        approve=False,
        reviewer_id=message.from_user.id,
        reason=reason,
        chat_id=message.chat.id,
        message_id=int(card_msg_id),
        caption=pending.get("caption") or "",
        storage=state.storage,
    )
    await _delete_mod_chat_messages(
        message.chat.id, prompt_message_id, message.message_id
    )
    if result != "ok":
        err = await message.reply(result[:180])
        await _delete_mod_chat_messages(message.chat.id, err.message_id)


def is_certificate_start(raw: str) -> bool:
    return looks_like_certificate_start(raw)
