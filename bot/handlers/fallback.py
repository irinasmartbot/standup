"""Last-resort handlers: unknown button presses → main menu."""

from __future__ import annotations

import logging

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery

from bot.db.analytics import EVENT_CMD_MAIN_MENU, track_event

logger = logging.getLogger(__name__)
router = Router(name="fallback")


@router.callback_query(F.message.chat.type == "private")
async def unknown_callback(call: CallbackQuery, state: FSMContext):
    """Старые/чужие inline-кнопки (Salebot и т.п.) → главное меню."""
    data = (call.data or "")[:80]
    logger.info(
        "Unknown callback → menu telegram_id=%s data=%r",
        call.from_user.id if call.from_user else None,
        data,
    )
    await state.clear()
    track_event(
        EVENT_CMD_MAIN_MENU,
        telegram_id=call.from_user.id,
        props={"via": "unknown_callback", "data": data or None},
    )
    try:
        await call.answer()
    except Exception:
        pass

    from bot.handlers.start import _delete_previous_menu_message, _send_welcome
    from bot.utils.bot_commands import refresh_user_commands

    if call.message:
        await _delete_previous_menu_message(call)
        await refresh_user_commands(call.message.bot, call.from_user.id)
        await _send_welcome(call.message)
