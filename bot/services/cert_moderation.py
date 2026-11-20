"""Фото сертификата в тот же чат модерации, что и скрины розыгрыша."""

from __future__ import annotations

import logging
from html import escape

from aiogram.types import BufferedInputFile
from aiogram.utils.keyboard import InlineKeyboardBuilder

from bot.config import MODERATION_CHAT_ID, bot

logger = logging.getLogger(__name__)


def moderation_chat_id() -> int | None:
    if not MODERATION_CHAT_ID:
        return None
    try:
        return int(MODERATION_CHAT_ID)
    except (TypeError, ValueError):
        return None


async def send_certificate_photo(
    *,
    session_id: int,
    code: str,
    channel: str,
    telegram_id: int | None,
    vk_id: int | None,
    username: str,
    full_name: str,
    photo,
) -> bool:
    chat_id = moderation_chat_id()
    if not chat_id:
        logger.error("MODERATION_CHAT_ID is not set")
        return False
    if channel == "vk" and vk_id is not None:
        who = (
            f'{escape(full_name or "Гость")} · '
            f'<a href="https://vk.com/id{int(vk_id)}">профиль VK</a> (id {int(vk_id)})'
        )
    else:
        uname = f"@{username}" if username else "без username"
        who = f"{escape(full_name or 'Гость')} {escape(uname)} (id {int(telegram_id or 0)})"
    caption = (
        f"{who} прислал СЕРТИФИКАТ\n"
        f"Номер: <code>{escape(code)}</code>\n"
        f"Заявка #{int(session_id)}"
    )
    kb = InlineKeyboardBuilder()
    kb.button(text="ПРИНЯТЬ", callback_data=f"cert_mod_ok_{int(session_id)}", style="success")
    kb.button(
        text="ОТКЛОНИТЬ",
        callback_data=f"cert_mod_no_{int(session_id)}",
        style="danger",
    )
    kb.adjust(1)
    try:
        kwargs = {
            "chat_id": chat_id,
            "caption": caption,
            "reply_markup": kb.as_markup(),
            "parse_mode": "HTML",
        }
        if isinstance(photo, (bytes, bytearray)):
            kwargs["photo"] = BufferedInputFile(bytes(photo), filename=f"certificate_{session_id}.jpg")
        else:
            kwargs["photo"] = photo
        await bot.send_photo(**kwargs)
        return True
    except Exception:
        logger.exception("Failed to send certificate photo to moderation")
        return False
