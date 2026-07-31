"""Сброс нижней Reply-клавиатуры (например «Поделиться номером»)."""

from aiogram.types import Message, ReplyKeyboardRemove


async def clear_reply_keyboard(message: Message) -> None:
    """Telegram не даёт совместить ReplyKeyboardRemove и inline в одном сообщении."""
    clear = await message.answer("⌨️", reply_markup=ReplyKeyboardRemove())
    try:
        await clear.delete()
    except Exception:
        pass
