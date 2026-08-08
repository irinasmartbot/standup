"""Helpers for Telegram callback queries when message is inaccessible."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

from aiogram.types import CallbackQuery, InaccessibleMessage, Message

from bot.config import bot


def message_text(message: Any) -> str:
    if message is None or isinstance(message, InaccessibleMessage):
        return ""
    return (getattr(message, "text", None) or getattr(message, "caption", None) or "") or ""


def reply_target(call: CallbackQuery):
    """Message-like object with answer/answer_photo even if callback message is gone."""
    msg = call.message
    if isinstance(msg, Message):
        return msg

    chat = getattr(msg, "chat", None)
    chat_id = chat.id if chat is not None else int(call.from_user.id)

    class _Target:
        def __init__(self) -> None:
            self.chat = chat if chat is not None else SimpleNamespace(id=chat_id)
            self.from_user = call.from_user
            self.message_id = int(getattr(msg, "message_id", 0) or 0)
            self.bot = call.bot

        async def answer(self, text: str, **kwargs):
            return await bot.send_message(self.chat.id, text, **kwargs)

        async def answer_photo(self, photo, caption=None, **kwargs):
            return await bot.send_photo(self.chat.id, photo, caption=caption, **kwargs)

        async def delete(self) -> None:
            mid = int(getattr(msg, "message_id", 0) or 0)
            if mid:
                await bot.delete_message(self.chat.id, mid)

    return _Target()
