"""Telegram callbacks for mailing follow-up buttons."""

from __future__ import annotations

import asyncio
import logging

from aiogram import F, Router
from aiogram.types import CallbackQuery

from bot.db.mailing import get_campaign_followup

logger = logging.getLogger(__name__)
router = Router()


@router.callback_query(F.data.startswith("mail_fu:"))
async def mailing_followup(call: CallbackQuery) -> None:
    # Сразу гасим «часики» на кнопке — до любого обращения к БД.
    raw = (call.data or "").split(":", 1)[-1].strip()
    if not raw.isdigit():
        await call.answer("Не найдено", show_alert=True)
        return
    try:
        await call.answer()
    except Exception:
        pass

    try:
        text = await asyncio.to_thread(get_campaign_followup, int(raw))
    except Exception:
        logger.exception("mailing followup db failed campaign=%s", raw)
        if call.message:
            await call.message.answer("Не удалось загрузить текст. Попробуйте ещё раз.")
        return

    if not text:
        if call.message:
            await call.message.answer(
                "Текст после кнопки не найден. Переотправьте тест или рассылку."
            )
        return

    if not call.message:
        return
    try:
        await call.message.answer(text, parse_mode="HTML")
    except Exception:
        try:
            await call.message.answer(text)
        except Exception:
            logger.exception("mailing followup send failed campaign=%s", raw)
