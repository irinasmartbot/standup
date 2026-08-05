"""Сброс нижней Reply-клавиатуры (например «Поделиться номером»)."""

from aiogram.types import Message, ReplyKeyboardRemove

# Чаты, где бот недавно показал Reply-клавиатуру. Без этого флага clear — no-op,
# чтобы меню не мигало служебным сообщением при каждом заходе.
_REPLY_KB_CHATS: set[int] = set()


def mark_reply_keyboard(chat_id: int) -> None:
    _REPLY_KB_CHATS.add(int(chat_id))


def reply_keyboard_marked(chat_id: int) -> bool:
    return int(chat_id) in _REPLY_KB_CHATS


async def clear_reply_keyboard(message: Message, *, force: bool = False) -> None:
    """Telegram не даёт совместить ReplyKeyboardRemove и inline в одном сообщении.

    Шлём короткое сообщение с Remove и сразу удаляем его — это даёт «подпрыгивание».
    Поэтому по умолчанию делаем только если клавиатуру реально показывали.
    """
    chat_id = int(message.chat.id)
    if not force and chat_id not in _REPLY_KB_CHATS:
        return
    clear = await message.answer("\u2060", reply_markup=ReplyKeyboardRemove())
    _REPLY_KB_CHATS.discard(chat_id)
    try:
        await clear.delete()
    except Exception:
        pass
