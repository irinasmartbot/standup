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
        await call.answer(
            "Текст после кнопки не найден. Переотправьте тест/рассылку.",
            show_alert=True,
        )
        return
    try:
        await call.answer()
        if call.message:
            try:
                await call.message.answer(text, parse_mode="HTML")
            except Exception:
                # Если HTML битый — шлём как обычный текст.
                await call.message.answer(text)
    except Exception:
        logger.exception("mailing followup failed campaign=%s", raw)
        try:
            await call.answer("Не удалось отправить", show_alert=True)
        except Exception:
            pass
