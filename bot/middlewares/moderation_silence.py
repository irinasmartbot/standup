"""В чате модерации бот не ведёт клиентские диалоги.

Разрешено только:
- callback ПРИНЯТЬ / ОТКЛОНИТЬ (rz_mod_*);
- reply с причиной отказа на карточку скрина после ОТКЛОНИТЬ.
"""

from typing import Any, Awaitable, Callable, Dict

from aiogram import BaseMiddleware
from aiogram.types import CallbackQuery, Message, TelegramObject

from bot.config import MODERATION_CHAT_ID


def _mod_chat_id():
    if not MODERATION_CHAT_ID:
        return None
    try:
        return int(MODERATION_CHAT_ID)
    except (TypeError, ValueError):
        return None


class ModerationChatSilenceMiddleware(BaseMiddleware):
    async def __call__(
        self,
        handler: Callable[[TelegramObject, Dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: Dict[str, Any],
    ) -> Any:
        mod_id = _mod_chat_id()
        if mod_id is None:
            return await handler(event, data)

        if isinstance(event, CallbackQuery) and event.message:
            if event.message.chat.id != mod_id:
                return await handler(event, data)
            if event.data and event.data.startswith("rz_mod_"):
                return await handler(event, data)
            try:
                await event.answer()
            except Exception:
                pass
            return None

        if isinstance(event, Message):
            if event.chat.id != mod_id:
                return await handler(event, data)
            # причина отказа — только reply на карточку после ОТКЛОНИТЬ
            if event.reply_to_message:
                from bot.handlers.rozygrysh import is_pending_reject_reply

                replied = event.reply_to_message
                if is_pending_reject_reply(replied.message_id):
                    return await handler(event, data)
                # reply на подсказку бота, которая сама reply на карточку
                if replied.reply_to_message and is_pending_reject_reply(replied.reply_to_message.message_id):
                    return await handler(event, data)
            return None

        return await handler(event, data)
