"""Telegram callbacks for mailing follow-up buttons."""

from __future__ import annotations

import logging

from aiogram import F, Router
from aiogram.types import CallbackQuery

from bot.db.mailing import get_campaign_followup

logger = logging.getLogger(__name__)
router = Router()


@router.callback_query(F.data.startswith("mail_fu:"))
async def mailing_followup(call: CallbackQuery) -> None:
    raw = (call.data or "").split(":", 1)[-1].strip()
    if not raw.isdigit():
        await call.answer("Не найдено", show_alert=True)
        return
    text = get_campaign_followup(int(raw))
    if not text:
        await call.answer("Сообщение недоступно", show_alert=True)
        return
    try:
        await call.message.answer(text, parse_mode="HTML")
        await call.answer()
    except Exception:
        logger.exception("mailing followup failed campaign=%s", raw)
        await call.answer("Не удалось отправить", show_alert=True)
