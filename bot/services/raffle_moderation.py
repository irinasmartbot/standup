"""Send raffle screenshots to the Telegram moderation chat.

Used by both Telegram and VK. Kept out of `bot.handlers.rozygrysh` so the VK
bot does not import the TG raffle router (aiogram handlers + nav_messages).
"""

from __future__ import annotations

import logging
from html import escape

from aiogram.utils.keyboard import InlineKeyboardBuilder

from bot.config import MODERATION_CHAT_ID, bot
from bot.db.crud import save_raffle_moderation_message

logger = logging.getLogger(__name__)


def moderation_chat_id() -> int | None:
    if not MODERATION_CHAT_ID:
        return None
    try:
        return int(MODERATION_CHAT_ID)
    except (TypeError, ValueError):
        return None


async def send_to_moderation(
    submission_id,
    telegram_id,
    username,
    full_name,
    kind,
    photo,
    *,
    vk_id=None,
) -> bool:
    chat_id = moderation_chat_id()
    if not chat_id:
        logger.error("MODERATION_CHAT_ID is not set or invalid")
        return False
    if kind not in {"post", "review"}:
        logger.error("Refusing moderation post with invalid kind=%s", kind)
        return False
    kind_label = "отзыва" if kind == "review" else "поста"
    if vk_id is not None:
        name = escape(full_name or "Гость")
        caption = (
            f'{name} · <a href="https://vk.com/id{int(vk_id)}">профиль VK</a> '
            f"(id {int(vk_id)}) прислал СКРИН {kind_label}\n"
            f"Заявка #{submission_id}"
        )
    else:
        uname = f"@{username}" if username else "без username"
        tg_id = int(telegram_id) if telegram_id is not None else 0
        caption = (
            f"{escape(full_name or 'Гость')} {escape(uname)} "
            f"(id {tg_id}) прислал СКРИН {kind_label}\n"
            f"Заявка #{submission_id}"
        )
    kb = InlineKeyboardBuilder()
    kb.button(text="ПРИНЯТЬ", callback_data=f"rz_mod_ok_{submission_id}", style="success")
    kb.button(
        text="ОТКЛОНИТЬ без комментария",
        callback_data=f"rz_mod_no_silent_{submission_id}",
        style="danger",
    )
    kb.button(
        text="ОТКЛОНИТЬ с комментарием",
        callback_data=f"rz_mod_no_reason_{submission_id}",
    )
    kb.adjust(1)
    try:
        # Только наша карточка модерации — без forward произвольных сообщений клиента
        sent = await bot.send_photo(
            chat_id=chat_id,
            photo=photo,
            caption=caption,
            reply_markup=kb.as_markup(),
            parse_mode="HTML",
        )
        # Сохраняем TG file_id с карточки модерации — по нему админка может показать превью
        # (в т.ч. для VK-заявок, где в БД сначала лежал vk-ref, а не file_id бота).
        tg_file_id = None
        if getattr(sent, "photo", None):
            try:
                tg_file_id = sent.photo[-1].file_id
            except (IndexError, TypeError, AttributeError):
                tg_file_id = None
        save_raffle_moderation_message(
            submission_id,
            chat_id,
            sent.message_id,
            photo_file_id=tg_file_id,
        )
        return True
    except Exception:
        logger.exception("Failed to send screenshot to moderation chat %s", chat_id)
        return False
