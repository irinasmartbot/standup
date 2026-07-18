# Последнее сообщение выбора дат/локаций по чату (не карточки мероприятий).

_BOOKING_NAV_BY_CHAT = {}


def remember_booking_nav(chat_id: int, message_id: int):
    _BOOKING_NAV_BY_CHAT[chat_id] = message_id


def forget_booking_nav(chat_id: int, message_id: int | None = None):
    current = _BOOKING_NAV_BY_CHAT.get(chat_id)
    if current is None:
        return
    if message_id is None or current == message_id:
        _BOOKING_NAV_BY_CHAT.pop(chat_id, None)


async def delete_booking_nav(bot, chat_id: int):
    message_id = _BOOKING_NAV_BY_CHAT.pop(chat_id, None)
    if not message_id:
        return
    try:
        await bot.delete_message(chat_id, message_id)
    except Exception:
        pass
