"""Подтягивает имя и @username из Telegram в users при каждом касании в личке."""

from __future__ import annotations

import logging
from typing import Any, Awaitable, Callable, Dict

from aiogram import BaseMiddleware
from aiogram.types import CallbackQuery, Message, TelegramObject

logger = logging.getLogger(__name__)


def _full_name(user) -> str:
    if not user:
        return ""
    parts = [getattr(user, "first_name", None) or "", getattr(user, "last_name", None) or ""]
    return " ".join(p for p in parts if p).strip()


class TouchUserProfileMiddleware(BaseMiddleware):
    async def __call__(
        self,
        handler: Callable[[TelegramObject, Dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: Dict[str, Any],
    ) -> Any:
        user = None
        is_private = False
        if isinstance(event, Message):
            user = event.from_user
            is_private = bool(event.chat and event.chat.type == "private")
        elif isinstance(event, CallbackQuery):
            user = event.from_user
            chat = event.message.chat if event.message else None
            is_private = bool(chat and chat.type == "private")

        if user and is_private:
            try:
                from bot.db.crud import touch_user_profile

                touch_user_profile(
                    telegram_id=user.id,
                    username=user.username,
                    name=_full_name(user),
                    source="telegram",
                )
            except Exception:
                logger.exception("touch_user_profile failed for tg=%s", getattr(user, "id", None))

        return await handler(event, data)
